"""Build aggTrade features for the exact 1m waterfall trade sample.

This script is intentionally trade-centric.  Earlier agg experiments used
generic true/fake waterfall event windows, which are useful for discovery but
do not align one-to-one with the executable 1m strategy.  Here each row is one
backtested 1m trade, with the aggTrade microstructure around that trade's
signal minute attached.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_experiments.build_agg_event_features import concat_symbol_trades, read_agg_zip, window_features


MINUTE_MS = 60_000

WINDOWS_MS = {
    "pre10m": (-600_000, 0),
    "pre5m": (-300_000, 0),
    "pre2m": (-120_000, 0),
    "pre60s": (-60_000, 0),
    "pre30s": (-30_000, 0),
    "pre10s": (-10_000, 0),
    "full_signal_1m": (0, 60_000),
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trades = pd.read_csv(args.trades)
    if args.start:
        start = pd.Timestamp(args.start, tz="UTC")
        trades = trades[pd.to_datetime(trades["signal_time"], unit="ms", utc=True) >= start]
    if args.end:
        end = pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)
        trades = trades[pd.to_datetime(trades["signal_time"], unit="ms", utc=True) < end]
    if args.max_rows > 0:
        trades = trades.head(args.max_rows).copy()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    missing = 0
    total = len(trades)
    for idx, (symbol, group) in enumerate(trades.groupby("symbol"), 1):
        group = group.sort_values("signal_time")
        trades_by_day = load_symbol_days(Path(args.agg_dir), str(symbol), group)
        agg = concat_symbol_trades(trades_by_day)
        if agg is None:
            missing += len(group)
            continue
        for trade in group.itertuples(index=False):
            row = build_row(trade, agg)
            if row:
                rows.append(row)
            else:
                missing += 1
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed symbols {idx}/{trades['symbol'].nunique()} rows={len(rows)} missing={missing}", flush=True)

    result = pd.DataFrame(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    feature_path = out_dir / f"agg_waterfall_trade_features_{stamp}.csv"
    result.to_csv(feature_path, index=False)
    summary = summarize(result, total, missing)
    summary_path = out_dir / f"agg_waterfall_trade_features_summary_{stamp}.json"
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


def load_symbol_days(agg_dir: Path, symbol: str, trades: pd.DataFrame) -> dict[date, pd.DataFrame]:
    days: set[date] = set()
    for row in trades.itertuples(index=False):
        signal_time = int(row.signal_time)
        start = signal_time - 10 * MINUTE_MS
        end = signal_time + MINUTE_MS
        d = datetime.fromtimestamp(start / 1000, tz=timezone.utc).date()
        end_day = datetime.fromtimestamp(end / 1000, tz=timezone.utc).date()
        while d <= end_day:
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


def build_row(trade: Any, agg: dict[str, np.ndarray]) -> dict[str, Any] | None:
    signal_time = int(trade.signal_time)
    if not has_trades(agg, signal_time, signal_time + MINUTE_MS):
        return None
    row: dict[str, Any] = {
        "symbol": str(trade.symbol),
        "rule": str(trade.rule),
        "family": str(trade.family),
        "signal_time": signal_time,
        "signal_iso": iso_ms(signal_time),
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
    for name, (start_offset, end_offset) in WINDOWS_MS.items():
        row.update(window_features(agg, signal_time + start_offset, signal_time + end_offset, name))
    for cutoff in (10_000, 20_000, 30_000, 40_000, 50_000, 59_999):
        row.update(window_features(agg, signal_time, signal_time + cutoff, f"m0_{cutoff // 1000}s"))
    return row


def has_trades(agg: dict[str, np.ndarray], start_ms: int, end_ms: int) -> bool:
    times = agg["time"]
    left = int(np.searchsorted(times, start_ms, side="left"))
    right = int(np.searchsorted(times, end_ms, side="right"))
    return right > left


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


def iso_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
