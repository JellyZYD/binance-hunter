"""Streaming aggTrade feature extraction for exact waterfall trades.

Unlike build_agg_features_for_waterfall_trades.py, this version does not load
whole daily aggTrade files into pandas.  It streams each zip once and only
updates small buckets for the target trade windows.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_experiments.replay_aggtrade_waterfall import iter_zip_trades


MINUTE_MS = 60_000
DAY_MS = 86_400_000

WINDOWS_MS = {
    "pre10m": (-600_000, 0),
    "pre5m": (-300_000, 0),
    "pre2m": (-120_000, 0),
    "pre60s": (-60_000, 0),
    "pre30s": (-30_000, 0),
    "pre10s": (-10_000, 0),
    "full_signal_1m": (0, 60_000),
    "m0_10s": (0, 10_000),
    "m0_20s": (0, 20_000),
    "m0_30s": (0, 30_000),
    "m0_40s": (0, 40_000),
    "m0_50s": (0, 50_000),
    "m0_59s": (0, 59_999),
}


@dataclass
class WindowRef:
    signal_time: int
    prefix: str
    start: int
    end: int


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
        features_by_signal = extract_symbol_features(Path(args.agg_dir), str(symbol), group)
        for trade in group.itertuples(index=False):
            signal_time = int(trade.signal_time)
            feats = features_by_signal.get(signal_time)
            if not feats or feats.get("full_signal_1m_trades", 0.0) <= 0:
                missing += 1
                continue
            rows.append(build_row(trade, feats))
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed symbols {idx}/{total_symbols} rows={len(rows)} missing={missing}", flush=True)

    result = pd.DataFrame(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    feature_path = out_dir / f"agg_waterfall_trade_features_fast_{stamp}.csv"
    result.to_csv(feature_path, index=False)
    summary = summarize(result, len(trades), missing)
    summary_path = out_dir / f"agg_waterfall_trade_features_fast_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"features": str(feature_path), "summary": str(summary_path), **summary}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--trades", required=True)
    p.add_argument("--agg-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--out-dir", default="backend/storage/ml/agg_waterfall_trade_features")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--progress-every", type=int, default=25)
    return p.parse_args(argv)


def extract_symbol_features(agg_dir: Path, symbol: str, group: pd.DataFrame) -> dict[int, dict[str, float]]:
    refs_by_day: dict[date, list[WindowRef]] = defaultdict(list)
    bucket_keys: dict[tuple[int, str], dict[str, Any]] = {}
    for row in group.itertuples(index=False):
        signal_time = int(row.signal_time)
        for prefix, (start_offset, end_offset) in WINDOWS_MS.items():
            start = signal_time + start_offset
            end = signal_time + end_offset
            ref = WindowRef(signal_time, prefix, start, end)
            start_day = datetime.fromtimestamp(start / 1000, tz=timezone.utc).date()
            end_day = datetime.fromtimestamp(end / 1000, tz=timezone.utc).date()
            day = start_day
            while day <= end_day:
                refs_by_day[day].append(ref)
                day += timedelta(days=1)
            bucket_keys[(signal_time, prefix)] = new_bucket()

    for day, refs in refs_by_day.items():
        path = agg_dir / symbol / f"{symbol}-aggTrades-{day.isoformat()}.zip"
        if not path.exists() or path.stat().st_size <= 0:
            continue
        refs = sorted(refs, key=lambda r: r.start)
        try:
            next_ref = 0
            active: list[WindowRef] = []
            for trade in iter_zip_trades(path):
                ts = int(trade["time"])
                while next_ref < len(refs) and refs[next_ref].start <= ts:
                    active.append(refs[next_ref])
                    next_ref += 1
                if active:
                    active = [ref for ref in active if ref.end >= ts]
                if not active and next_ref >= len(refs):
                    break
                price = float(trade["price"])
                qty = float(trade["qty"])
                quote = price * qty
                buyer_maker = bool(trade["buyer_maker"])
                for ref in active:
                    update_bucket(bucket_keys[(ref.signal_time, ref.prefix)], ts, ref.start, ref.end, price, quote, buyer_maker)
        except Exception:
            continue

    out: dict[int, dict[str, float]] = defaultdict(dict)
    for (signal_time, prefix), bucket in bucket_keys.items():
        out[signal_time].update(finalize_bucket(prefix, bucket))
    return dict(out)


def build_row(trade: Any, feats: dict[str, float]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": str(trade.symbol),
        "rule": str(trade.rule),
        "family": str(trade.family),
        "signal_time": int(trade.signal_time),
        "signal_iso": datetime.fromtimestamp(int(trade.signal_time) / 1000, tz=timezone.utc).isoformat(),
        "entry_time": int(trade.entry_time),
        "exit_time": int(trade.exit_time),
        "entry": float(trade.entry),
        "exit": float(trade.exit),
        "ret": float(trade.ret),
        "mae": float(trade.mae),
        "mfe": float(trade.mfe),
        "hold_min": int(trade.hold_min),
        "exit_reason": str(trade.exit_reason),
    }
    for name in [
        "ret_30m",
        "ret_2h",
        "ret_4h",
        "ret_12h",
        "ret_24h",
        "runup_24h",
        "dd_from_24h_high",
        "qv30",
        "volr20",
        "volr5_20",
        "tsell",
        "tsell5",
        "body_drop",
        "drop_2m",
        "drop_5m",
        "close_pos",
        "upper_wick",
        "lower_wick",
        "range_pct",
        "break_depth",
        "red_streak",
        "qv_over_prev6max",
    ]:
        if hasattr(trade, name):
            row[name] = safe_float(getattr(trade, name))
    row.update(feats)
    return row


def new_bucket() -> dict[str, Any]:
    return {
        "trades": 0,
        "quote": 0.0,
        "sell_quote": 0.0,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "low_time": 0,
        "start": 0,
        "end": 0,
    }


def update_bucket(b: dict[str, Any], ts: int, start: int, end: int, price: float, quote: float, buyer_maker: bool) -> None:
    if b["trades"] == 0:
        b["open"] = price
        b["high"] = price
        b["low"] = price
        b["low_time"] = ts
        b["start"] = start
        b["end"] = end
    b["trades"] += 1
    b["quote"] += quote
    if buyer_maker:
        b["sell_quote"] += quote
    b["high"] = max(float(b["high"]), price)
    if price <= float(b["low"]):
        b["low"] = price
        b["low_time"] = ts
    b["close"] = price


def finalize_bucket(prefix: str, b: dict[str, Any]) -> dict[str, float]:
    if int(b["trades"]) <= 0:
        return {
            f"{prefix}_trades": 0.0,
            f"{prefix}_quote": 0.0,
            f"{prefix}_sell_ratio": 0.0,
            f"{prefix}_ret": 0.0,
            f"{prefix}_range": 0.0,
            f"{prefix}_close_pos": 0.5,
            f"{prefix}_rebound_from_low": 0.0,
            f"{prefix}_low_time_frac": 0.0,
        }
    open_ = float(b["open"])
    high = float(b["high"])
    low = float(b["low"])
    close = float(b["close"])
    quote = float(b["quote"])
    span = max(1, int(b["end"]) - int(b["start"]))
    return {
        f"{prefix}_trades": float(b["trades"]),
        f"{prefix}_quote": quote,
        f"{prefix}_sell_ratio": float(b["sell_quote"]) / quote if quote else 0.0,
        f"{prefix}_ret": close / open_ - 1.0 if open_ > 0 else 0.0,
        f"{prefix}_range": high / low - 1.0 if low > 0 else 0.0,
        f"{prefix}_close_pos": (close - low) / (high - low) if high > low else 0.5,
        f"{prefix}_rebound_from_low": close / low - 1.0 if low > 0 else 0.0,
        f"{prefix}_low_time_frac": float(int(b["low_time"]) - int(b["start"])) / span,
    }


def summarize(df: pd.DataFrame, requested: int, missing: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "requested_trades": int(requested),
        "feature_rows": int(len(df)),
        "missing_trades": int(missing),
    }
    if not df.empty:
        out["symbols"] = int(df["symbol"].nunique())
        out["families"] = {str(k): int(v) for k, v in df.groupby("family").size().to_dict().items()}
        out["rules"] = {str(k): int(v) for k, v in df.groupby("rule").size().to_dict().items()}
    return out


def safe_float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return float("nan")
    return out if np.isfinite(out) else float("nan")


if __name__ == "__main__":
    raise SystemExit(main())
