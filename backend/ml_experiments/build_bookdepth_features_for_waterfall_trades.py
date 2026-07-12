"""Append Binance Vision bookDepth features to executable waterfall trades.

The public bookDepth archive is a coarse percentage-depth snapshot, normally
around every 30 seconds.  This extractor does not try to reconstruct a full
order book.  It builds features that are useful for the waterfall question:
whether nearby bid depth is being pulled and whether ask depth dominates.
"""
from __future__ import annotations

import argparse
import json
import math
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PCTS = (1, 2, 3, 4, 5)
CUTOFFS_MS = {
    "bd_pre10m": -600_000,
    "bd_pre5m": -300_000,
    "bd_pre2m": -120_000,
    "bd_pre60s": -60_000,
    "bd_pre30s": -30_000,
    "bd_m0_10s": 10_000,
    "bd_m0_30s": 30_000,
    "bd_m0_40s": 40_000,
    "bd_m0_50s": 50_000,
    "bd_m0_59s": 59_999,
}


@dataclass(frozen=True)
class CutoffRef:
    signal_time: int
    prefix: str
    cutoff: int


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trades = pd.read_csv(args.trades)
    if args.start:
        trades = trades[pd.to_datetime(trades["signal_time"], unit="ms", utc=True) >= pd.Timestamp(args.start, tz="UTC")]
    if args.end:
        trades = trades[pd.to_datetime(trades["signal_time"], unit="ms", utc=True) < pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)]
    if args.max_rows > 0:
        trades = trades.head(args.max_rows).copy()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    missing = 0
    total_symbols = trades["symbol"].nunique()
    for idx, (symbol, group) in enumerate(trades.groupby("symbol"), 1):
        features_by_signal = extract_symbol_features(Path(args.bookdepth_dir), str(symbol), group)
        for trade in group.itertuples(index=False):
            row = dict(trade._asdict())
            signal_time = int(row["signal_time"])
            feats = features_by_signal.get(signal_time)
            if not feats or feats.get("bd_m0_59s_age_sec", np.inf) > args.max_age_sec:
                missing += 1
                row.update(empty_features())
            else:
                row.update(feats)
            rows.append(row)
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed symbols {idx}/{total_symbols} rows={len(rows)} missing={missing}", flush=True)

    result = pd.DataFrame(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    feature_path = out_dir / f"bookdepth_waterfall_trade_features_{stamp}.csv"
    result.to_csv(feature_path, index=False)
    summary = summarize(result, len(trades), missing)
    summary_path = out_dir / f"bookdepth_waterfall_trade_features_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"features": str(feature_path), "summary": str(summary_path), **summary}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--trades", required=True)
    p.add_argument("--bookdepth-dir", default="backend/storage/bookdepth/binance_vision")
    p.add_argument("--out-dir", default="backend/storage/ml/bookdepth_waterfall_trade_features")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--max-age-sec", type=float, default=75.0)
    p.add_argument("--progress-every", type=int, default=25)
    return p.parse_args(argv)


def extract_symbol_features(bookdepth_dir: Path, symbol: str, group: pd.DataFrame) -> dict[int, dict[str, float]]:
    refs_by_day: dict[date, list[CutoffRef]] = {}
    for row in group.itertuples(index=False):
        signal_time = int(row.signal_time)
        for prefix, offset in CUTOFFS_MS.items():
            cutoff = signal_time + offset
            day = datetime.fromtimestamp(cutoff / 1000, tz=timezone.utc).date()
            refs_by_day.setdefault(day, []).append(CutoffRef(signal_time, prefix, cutoff))

    out: dict[int, dict[str, float]] = {}
    for day, refs in refs_by_day.items():
        depth = read_bookdepth_day(bookdepth_dir, symbol, day)
        if depth.empty:
            continue
        refs = sorted(refs, key=lambda r: r.cutoff)
        snap_times = np.array(sorted(depth["ts"].unique()), dtype=np.int64)
        for ref in refs:
            pos = np.searchsorted(snap_times, ref.cutoff, side="right") - 1
            if pos < 0:
                continue
            ts = int(snap_times[pos])
            snap = depth[depth["ts"] == ts]
            feats = snapshot_features(ref.prefix, snap, ref.cutoff, ts)
            out.setdefault(ref.signal_time, {}).update(feats)
    for signal_time, feats in out.items():
        feats.update(delta_features(feats))
    return out


def read_bookdepth_day(bookdepth_dir: Path, symbol: str, day: date) -> pd.DataFrame:
    path = bookdepth_dir / symbol / f"{symbol}-bookDepth-{day.isoformat()}.zip"
    if not path.exists() or path.stat().st_size <= 0:
        return pd.DataFrame()
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if not names:
                return pd.DataFrame()
            with zf.open(names[0]) as f:
                df = pd.read_csv(f)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    # Pandas may infer second-resolution dtype for these files.  Normalize to
    # ns before converting, otherwise the value can be 1000x too small.
    ts_ns = ts.dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]")
    df["ts"] = (ts_ns.astype("int64") // 1_000_000).where(ts.notna())
    for col in ("percentage", "depth", "notional"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["ts", "percentage", "depth", "notional"]).sort_values(["ts", "percentage"])


def snapshot_features(prefix: str, snap: pd.DataFrame, cutoff: int, snap_ts: int) -> dict[str, float]:
    out: dict[str, float] = {f"{prefix}_age_sec": max(0.0, (cutoff - snap_ts) / 1000.0)}
    ref_estimates: list[float] = []
    for pct in PCTS:
        bid = pct_value(snap, -pct, "notional")
        ask = pct_value(snap, pct, "notional")
        bid_depth = pct_value(snap, -pct, "depth")
        ask_depth = pct_value(snap, pct, "depth")
        out[f"{prefix}_bid_{pct}pct"] = bid
        out[f"{prefix}_ask_{pct}pct"] = ask
        out[f"{prefix}_total_{pct}pct"] = bid + ask if np.isfinite(bid) and np.isfinite(ask) else np.nan
        out[f"{prefix}_imb_{pct}pct"] = safe_div(bid - ask, bid + ask)
        out[f"{prefix}_ask_bid_{pct}pct"] = safe_div(ask, bid)
        if bid_depth > 0 and bid > 0:
            ref_estimates.append((bid / bid_depth) / (1.0 - pct / 100.0))
        if ask_depth > 0 and ask > 0:
            ref_estimates.append((ask / ask_depth) / (1.0 + pct / 100.0))
    out[f"{prefix}_bid_slope_1_5"] = safe_div(out.get(f"{prefix}_bid_1pct", np.nan), out.get(f"{prefix}_bid_5pct", np.nan))
    out[f"{prefix}_ask_slope_1_5"] = safe_div(out.get(f"{prefix}_ask_1pct", np.nan), out.get(f"{prefix}_ask_5pct", np.nan))
    out[f"{prefix}_ref_price_cv"] = coeff_var(ref_estimates)
    return out


def pct_value(snap: pd.DataFrame, pct: int, col: str) -> float:
    exact = snap[np.isclose(snap["percentage"].astype(float), float(pct))]
    if exact.empty:
        return np.nan
    return float(exact[col].iloc[0])


def delta_features(feats: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for cur in ("bd_m0_30s", "bd_m0_40s", "bd_m0_50s", "bd_m0_59s"):
        for base in ("bd_pre2m", "bd_pre5m", "bd_pre10m"):
            for pct in (1, 2, 5):
                out[f"{cur}_bid_{pct}pct_logchg_vs_{base}"] = log_ratio(feats.get(f"{cur}_bid_{pct}pct"), feats.get(f"{base}_bid_{pct}pct"))
                out[f"{cur}_ask_{pct}pct_logchg_vs_{base}"] = log_ratio(feats.get(f"{cur}_ask_{pct}pct"), feats.get(f"{base}_ask_{pct}pct"))
                out[f"{cur}_imb_{pct}pct_delta_vs_{base}"] = safe_sub(feats.get(f"{cur}_imb_{pct}pct"), feats.get(f"{base}_imb_{pct}pct"))
                out[f"{cur}_ask_bid_{pct}pct_logchg_vs_{base}"] = log_ratio(feats.get(f"{cur}_ask_bid_{pct}pct"), feats.get(f"{base}_ask_bid_{pct}pct"))
    return out


def safe_div(a: Any, b: Any) -> float:
    try:
        aa = float(a)
        bb = float(b)
    except Exception:
        return np.nan
    if not np.isfinite(aa) or not np.isfinite(bb) or abs(bb) < 1e-12:
        return np.nan
    return aa / bb


def safe_sub(a: Any, b: Any) -> float:
    try:
        aa = float(a)
        bb = float(b)
    except Exception:
        return np.nan
    if not np.isfinite(aa) or not np.isfinite(bb):
        return np.nan
    return aa - bb


def log_ratio(a: Any, b: Any) -> float:
    r = safe_div(a, b)
    if not np.isfinite(r) or r <= 0:
        return np.nan
    return math.log(r)


def coeff_var(values: list[float]) -> float:
    vals = np.array([x for x in values if np.isfinite(x) and x > 0], dtype=float)
    if len(vals) < 2:
        return np.nan
    mean = float(vals.mean())
    return float(vals.std() / mean) if mean > 0 else np.nan


def empty_features() -> dict[str, float]:
    out: dict[str, float] = {}
    for prefix in CUTOFFS_MS:
        out.update(snapshot_features(prefix, pd.DataFrame(columns=["percentage", "depth", "notional"]), 0, 0))
    out.update(delta_features(out))
    return out


def summarize(result: pd.DataFrame, requested: int, missing: int) -> dict[str, Any]:
    return {
        "requested_trades": requested,
        "feature_rows": int(len(result)),
        "missing_m0_59s": int(missing),
        "symbols": int(result["symbol"].nunique()) if "symbol" in result else 0,
        "families": result["family"].value_counts().to_dict() if "family" in result else {},
        "median_book_age_sec": float(result["bd_m0_59s_age_sec"].median()) if "bd_m0_59s_age_sec" in result else None,
        "p95_book_age_sec": float(result["bd_m0_59s_age_sec"].quantile(0.95)) if "bd_m0_59s_age_sec" in result else None,
        "median_ref_price_cv": float(result["bd_m0_59s_ref_price_cv"].median()) if "bd_m0_59s_ref_price_cv" in result else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
