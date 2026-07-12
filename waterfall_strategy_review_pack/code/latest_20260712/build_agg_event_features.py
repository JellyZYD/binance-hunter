"""Build event-level aggTrade features for waterfall true/fake breaks.

Input events come from build_agg_event_windows.py.  This script only uses
downloaded Binance Vision aggTrade zips and emits one row per event with
microstructure features around the event timestamp.  It is a research dataset,
not production logic.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


WINDOWS_MS = {
    "pre5m": (-300_000, 0),
    "pre2m": (-120_000, 0),
    "pre60s": (-60_000, 0),
    "pre30s": (-30_000, 0),
    "pre10s": (-10_000, 0),
    "post10s": (0, 10_000),
    "post30s": (0, 30_000),
    "post60s": (0, 60_000),
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    events = pd.read_csv(args.events)
    if args.max_events > 0:
        events = events.head(args.max_events).copy()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    missing_events = 0
    for idx, (symbol, group) in enumerate(events.groupby("symbol"), 1):
        group = group.sort_values("event_time")
        trades_by_day = load_symbol_days(Path(args.agg_dir), str(symbol), group)
        trades = concat_symbol_trades(trades_by_day)
        if trades is None:
            missing_events += len(group)
            continue
        for event in group.itertuples(index=False):
            row = build_event_row(event, trades)
            if row:
                rows.append(row)
            else:
                missing_events += 1
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed symbols {idx}/{events['symbol'].nunique()} rows={len(rows)} missing={missing_events}", flush=True)

    result = pd.DataFrame(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"agg_event_features_{stamp}.csv"
    result.to_csv(path, index=False)
    summary = summarize(result, len(events), missing_events)
    summary_path = out_dir / f"agg_event_features_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"features": str(path), "summary": str(summary_path), **summary}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--agg-dir", default="backend/storage/aggtrades/binance_vision")
    parser.add_argument("--out-dir", default="backend/storage/ml/agg_event_features")
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=25)
    return parser.parse_args(argv)


def load_symbol_days(agg_dir: Path, symbol: str, events: pd.DataFrame) -> dict[date, pd.DataFrame]:
    days: set[date] = set()
    for row in events.itertuples(index=False):
        start = date.fromisoformat(str(row.agg_start_day))
        end = date.fromisoformat(str(row.agg_end_day))
        d = start
        while d <= end:
            days.add(d)
            d += timedelta(days=1)
    out: dict[date, pd.DataFrame] = {}
    for day in sorted(days):
        path = agg_dir / symbol / f"{symbol}-aggTrades-{day.isoformat()}.zip"
        if not path.exists() or path.stat().st_size <= 0:
            continue
        try:
            df = read_agg_zip(path)
            if not df.empty:
                out[day] = df
        except Exception:
            continue
    return out


def read_agg_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return pd.DataFrame()
        with zf.open(names[0]) as fh:
            raw = pd.read_csv(
                fh,
                header=None,
                usecols=[1, 2, 5, 6],
                names=["price", "qty", "time", "buyer_maker"],
                low_memory=False,
            )
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce")
    raw["qty"] = pd.to_numeric(raw["qty"], errors="coerce")
    raw["time"] = pd.to_numeric(raw["time"], errors="coerce")
    raw = raw.dropna(subset=["price", "qty", "time"])
    if raw.empty:
        return raw
    raw["time"] = raw["time"].astype(np.int64)
    raw["quote"] = raw["price"].astype(float) * raw["qty"].astype(float)
    maker = raw["buyer_maker"].astype(str).str.lower().eq("true")
    raw["sell_quote"] = np.where(maker, raw["quote"], 0.0)
    raw["buy_quote"] = np.where(maker, 0.0, raw["quote"])
    return raw[["time", "price", "qty", "quote", "sell_quote", "buy_quote"]].sort_values("time").reset_index(drop=True)


def concat_symbol_trades(trades_by_day: dict[date, pd.DataFrame]) -> dict[str, np.ndarray] | None:
    if not trades_by_day:
        return None
    frames = [df for _, df in sorted(trades_by_day.items()) if not df.empty]
    if not frames:
        return None
    trades = pd.concat(frames, ignore_index=True).sort_values("time")
    return {
        "time": trades["time"].astype(np.int64).to_numpy(),
        "price": trades["price"].astype(float).to_numpy(),
        "quote": trades["quote"].astype(float).to_numpy(),
        "sell_quote": trades["sell_quote"].astype(float).to_numpy(),
    }


def build_event_row(event: Any, trades: dict[str, np.ndarray]) -> dict[str, Any]:
    event_time = int(event.event_time)
    min_start = int(event_time - event_time % 60_000)
    row = {
        "symbol": str(event.symbol),
        "label": str(event.label),
        "family": str(event.family),
        "event_time": event_time,
        "event_iso": str(event.event_iso),
        "event_price": float(event.event_price),
        "future_drop_5m": float(event.future_drop_5m),
        "future_drop_15m": float(event.future_drop_15m),
        "future_drop_30m": float(event.future_drop_30m),
        "future_drop_60m": float(event.future_drop_60m),
        "adverse_5m": float(event.adverse_5m),
        "adverse_15m": float(event.adverse_15m),
        "ret_30m": float(event.ret_30m),
        "ret_2h": float(event.ret_2h),
        "ret_4h": float(event.ret_4h),
        "ret_12h": float(event.ret_12h),
        "ret_24h": float(event.ret_24h),
        "runup_24h": float(event.runup_24h),
        "dd_from_24h_high": float(event.dd_from_24h_high),
        "qv30": float(event.qv30),
        "volr20": float(event.volr20),
        "volr5_20": float(event.volr5_20),
        "tsell": float(event.tsell),
        "body_drop": float(event.body_drop),
        "drop_2m": float(event.drop_2m),
        "drop_5m": float(event.drop_5m),
        "close_pos": float(event.close_pos),
        "range_pct": float(event.range_pct),
        "break_depth": float(event.break_depth),
    }
    for name, (offset_start, offset_end) in WINDOWS_MS.items():
        row.update(window_features(trades, event_time + offset_start, event_time + offset_end, name))
    for cutoff in (10_000, 20_000, 30_000, 40_000, 50_000, 59_999):
        row.update(window_features(trades, min_start, min_start + cutoff, f"m0_{cutoff // 1000}s"))
    return row


def window_features(trades: dict[str, np.ndarray], start_ms: int, end_ms: int, prefix: str) -> dict[str, float]:
    times = trades["time"]
    left = int(np.searchsorted(times, start_ms, side="left"))
    right = int(np.searchsorted(times, end_ms, side="right"))
    if right <= left:
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
    prices = trades["price"][left:right]
    event_times = times[left:right]
    quote_values = trades["quote"][left:right]
    sell_values = trades["sell_quote"][left:right]
    quote = float(np.sum(quote_values))
    sell = float(np.sum(sell_values))
    first = float(prices[0])
    last = float(prices[-1])
    high = float(np.max(prices))
    low = float(np.min(prices))
    low_pos = int(np.argmin(prices))
    span = max(1, end_ms - start_ms)
    return {
        f"{prefix}_trades": float(right - left),
        f"{prefix}_quote": quote,
        f"{prefix}_sell_ratio": sell / quote if quote > 0 else 0.0,
        f"{prefix}_ret": last / first - 1.0 if first > 0 else 0.0,
        f"{prefix}_range": high / low - 1.0 if low > 0 else 0.0,
        f"{prefix}_close_pos": (last - low) / (high - low) if high > low else 0.5,
        f"{prefix}_rebound_from_low": last / low - 1.0 if low > 0 else 0.0,
        f"{prefix}_low_time_frac": float(event_times[low_pos] - start_ms) / span,
    }


def summarize(df: pd.DataFrame, requested: int, missing: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "requested_events": int(requested),
        "feature_rows": int(len(df)),
        "missing_events": int(missing),
    }
    if not df.empty:
        out["symbols"] = int(df["symbol"].nunique())
        out["labels"] = {str(k): int(v) for k, v in df.groupby("label").size().to_dict().items()}
        out["families"] = {str(k): int(v) for k, v in df.groupby("family").size().to_dict().items()}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
