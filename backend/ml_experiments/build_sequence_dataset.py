from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pump_dump_hunter.config import load_settings
from pump_dump_hunter.data.bb_importer import DEFAULT_NON_ALT_SYMBOLS


KLINE_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "quote_volume",
    "taker_buy_quote_volume",
]


def main() -> int:
    args = parse_args()
    source = Path(args.source)
    out = Path(args.out)
    settings = load_settings(args.config)
    symbols = discover_symbols(source, settings, args.symbols, args.max_symbols)
    if not symbols:
      raise SystemExit("no usable symbols")

    all_x: list[np.ndarray] = []
    all_top: list[np.ndarray] = []
    all_dump: list[np.ndarray] = []
    all_ts: list[np.ndarray] = []
    all_symbol: list[np.ndarray] = []
    all_metrics: list[np.ndarray] = []
    feature_names: list[str] | None = None
    used_symbols: list[str] = []
    skipped: list[str] = []

    for symbol_id, symbol in enumerate(symbols):
        path = source / "klines" / f"{symbol}.parquet"
        try:
            df = read_klines(path, args.days)
            bars = aggregate_15m(df, args.min_minutes_per_bar)
            if args.include_state:
                bars = add_state_features(source, symbol, bars)
            x, y_top, y_dump, ts, metrics, names = build_symbol_samples(bars, args)
        except Exception as exc:
            skipped.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:120]}")
            continue
        if len(x) == 0:
            skipped.append(f"{symbol}:no_samples")
            continue
        if feature_names is None:
            feature_names = names
        all_x.append(x)
        all_top.append(y_top)
        all_dump.append(y_dump)
        all_ts.append(ts)
        all_symbol.append(np.full(len(x), symbol_id, dtype=np.int16))
        all_metrics.append(metrics)
        used_symbols.append(symbol)
        print(f"{symbol} samples={len(x)}", flush=True)

    if not all_x:
        raise SystemExit("no samples built")

    x = np.concatenate(all_x, axis=0).astype(np.float32)
    y_top = np.concatenate(all_top).astype(np.int8)
    y_dump = np.concatenate(all_dump).astype(np.int8)
    ts = np.concatenate(all_ts).astype(np.int64)
    symbol_ids = np.concatenate(all_symbol).astype(np.int16)
    metrics = np.concatenate(all_metrics, axis=0).astype(np.float32)

    order = np.argsort(ts, kind="mergesort")
    x = x[order]
    y_top = y_top[order]
    y_dump = y_dump[order]
    ts = ts[order]
    symbol_ids = symbol_ids[order]
    metrics = metrics[order]

    if args.max_samples and len(x) > args.max_samples:
        idx = np.linspace(0, len(x) - 1, args.max_samples).astype(np.int64)
        x = x[idx]
        y_top = y_top[idx]
        y_dump = y_dump[idx]
        ts = ts[idx]
        symbol_ids = symbol_ids[idx]
        metrics = metrics[idx]

    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        x=x,
        y_top=y_top,
        y_dump=y_dump,
        timestamp=ts,
        symbol_id=symbol_ids,
        metrics=metrics,
        feature_names=np.array(feature_names or [], dtype=object),
        metric_names=np.array(["drop_24h", "up_24h", "drop_12h", "up_12h", "drop_48h", "up_48h"], dtype=object),
        symbol_names=np.array(used_symbols, dtype=object),
    )
    meta = {
        "source": str(source),
        "out": str(out),
        "symbols": len(used_symbols),
        "samples": int(len(x)),
        "seq_len": args.seq_len,
        "features": len(feature_names or []),
        "y_top_positive_rate": float(y_top.mean()),
        "y_dump_positive_rate": float(y_dump.mean()),
        "skipped": skipped[:200],
        "skipped_count": len(skipped),
        "args": vars(args),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build 15m sequence dataset for pump-dump ML experiments.")
    parser.add_argument("--source", default=r"E:\A\bb\data", help="Data root containing klines/, funding/, market_state_hist/.")
    parser.add_argument("--out", default="storage/ml/sequence_dataset.npz")
    parser.add_argument("--config", default="config/settings.json")
    parser.add_argument("--symbols", default="", help="Comma separated symbols. Empty means discover from source.")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--seq-len", type=int, default=64)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--runup-window", type=int, default=96)
    parser.add_argument("--runup-min", type=float, default=0.20)
    parser.add_argument("--top-horizon", type=int, default=96, help="24h on 15m bars.")
    parser.add_argument("--dump-horizon", type=int, default=48, help="12h on 15m bars.")
    parser.add_argument("--eval-horizon", type=int, default=192, help="48h on 15m bars.")
    parser.add_argument("--top-drop", type=float, default=0.08)
    parser.add_argument("--top-adverse", type=float, default=0.08)
    parser.add_argument("--dump-drop", type=float, default=0.08)
    parser.add_argument("--dump-adverse", type=float, default=0.04)
    parser.add_argument("--min-minutes-per-bar", type=int, default=10)
    parser.add_argument("--include-state", action="store_true", help="Merge funding/OI/ratio features if present.")
    parser.add_argument("--max-samples", type=int, default=0)
    return parser.parse_args()


def discover_symbols(source: Path, settings: dict[str, Any], raw_symbols: str, max_symbols: int) -> list[str]:
    excluded = {str(s).upper() for s in settings.get("universe", {}).get("exclude_symbols", [])}
    excluded |= DEFAULT_NON_ALT_SYMBOLS
    if raw_symbols.strip():
        symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
    else:
        symbols = sorted(p.stem.upper() for p in (source / "klines").glob("*.parquet"))
    symbols = [s for s in symbols if s.isascii() and s.isalnum() and s not in excluded]
    return symbols[:max_symbols] if max_symbols else symbols


def read_klines(path: Path, days: int) -> pd.DataFrame:
    pf = pq.ParquetFile(path)
    max_ts = parquet_max_timestamp(pf)
    filters = None
    if days > 0 and max_ts is not None:
        start = int(max_ts) - int(days) * 86_400_000 + 1
        filters = [("timestamp", ">=", start)]
    table = pq.read_table(path, columns=KLINE_COLUMNS, filters=filters)
    df = table.to_pandas()
    if df.empty:
        return df
    return df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")


def parquet_max_timestamp(pf: pq.ParquetFile) -> int | None:
    names = pf.schema_arrow.names
    if "timestamp" not in names:
        return None
    idx = names.index("timestamp")
    values = []
    for i in range(pf.metadata.num_row_groups):
        stats = pf.metadata.row_group(i).column(idx).statistics
        if stats and stats.has_min_max:
            values.append(int(stats.max))
    return max(values) if values else None


def aggregate_15m(df: pd.DataFrame, min_minutes: int) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["bucket"] = (out["timestamp"].astype("int64") // 900_000) * 900_000
    bars = out.groupby("bucket", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        quote_volume=("quote_volume", "sum"),
        taker_buy_quote_volume=("taker_buy_quote_volume", "sum"),
        count=("timestamp", "count"),
    )
    bars = bars[bars["count"] >= min_minutes].reset_index().rename(columns={"bucket": "timestamp"})
    return bars


def add_state_features(source: Path, symbol: str, bars: pd.DataFrame) -> pd.DataFrame:
    result = bars.sort_values("timestamp").copy()
    state_specs = [
        ("funding", "funding_rate", "funding_rate"),
        ("market_state_hist/oi", "oi", "oi"),
        ("market_state_hist/oi", "oi_value", "oi_value"),
        ("market_state_hist/top_pos_ratio", "ratio", "top_pos_ratio"),
        ("market_state_hist/global_acct_ratio", "ratio", "global_acct_ratio"),
        ("market_state_hist/taker_ratio", "ratio", "taker_ratio"),
    ]
    for rel, col, name in state_specs:
        path = source / rel / f"{symbol}.parquet"
        if not path.exists():
            result[name] = 0.0
            continue
        try:
            state = pq.read_table(path, columns=["timestamp", col]).to_pandas()
        except Exception:
            result[name] = 0.0
            continue
        if state.empty:
            result[name] = 0.0
            continue
        state = state.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").rename(columns={col: name})
        result = pd.merge_asof(result, state[["timestamp", name]], on="timestamp", direction="backward")
        result[name] = result[name].ffill().fillna(0.0)
    return result


def build_symbol_samples(bars: pd.DataFrame, args: argparse.Namespace) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    min_required = max(args.seq_len, args.runup_window, args.eval_horizon) + 8
    if len(bars) < min_required:
        return empty_samples(args.seq_len), np.array([], dtype=np.int8), np.array([], dtype=np.int8), np.array([], dtype=np.int64), np.empty((0, 6)), []

    features, names = make_features(bars)
    close = bars["close"].to_numpy(dtype=np.float64)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    timestamps = (bars["timestamp"].to_numpy(dtype=np.int64) + 899_999).astype(np.int64)

    rolling_low = pd.Series(low).rolling(args.runup_window, min_periods=args.runup_window).min().to_numpy()
    runup = close / rolling_low - 1.0
    fut_low_top = future_min(low, args.top_horizon)
    fut_high_top = future_max(high, args.top_horizon)
    fut_low_dump = future_min(low, args.dump_horizon)
    fut_high_dump = future_max(high, args.dump_horizon)
    fut_low_eval = future_min(low, args.eval_horizon)
    fut_high_eval = future_max(high, args.eval_horizon)

    drop_top = close / fut_low_top - 1.0
    up_top = fut_high_top / close - 1.0
    drop_dump = close / fut_low_dump - 1.0
    up_dump = fut_high_dump / close - 1.0
    drop_eval = close / fut_low_eval - 1.0
    up_eval = fut_high_eval / close - 1.0

    start = max(args.seq_len - 1, args.runup_window)
    end = len(bars) - args.eval_horizon
    sample_idx = np.arange(start, end, max(1, args.stride), dtype=np.int64)
    finite_row = np.isfinite(features).all(axis=1).astype(int)
    finite_seq = pd.Series(finite_row).rolling(args.seq_len, min_periods=args.seq_len).sum().to_numpy() == args.seq_len
    valid = (
        finite_seq[sample_idx]
        & np.isfinite(runup[sample_idx])
        & (runup[sample_idx] >= args.runup_min)
        & np.isfinite(drop_top[sample_idx])
        & np.isfinite(up_top[sample_idx])
        & np.isfinite(drop_dump[sample_idx])
        & np.isfinite(up_dump[sample_idx])
    )
    sample_idx = sample_idx[valid]
    if len(sample_idx) == 0:
        return empty_samples(args.seq_len, features.shape[1]), np.array([], dtype=np.int8), np.array([], dtype=np.int8), np.array([], dtype=np.int64), np.empty((0, 6)), names

    x = np.stack([features[i - args.seq_len + 1 : i + 1] for i in sample_idx]).astype(np.float32)
    y_top = ((drop_top[sample_idx] >= args.top_drop) & (up_top[sample_idx] <= args.top_adverse)).astype(np.int8)
    y_dump = ((drop_dump[sample_idx] >= args.dump_drop) & (up_dump[sample_idx] <= args.dump_adverse)).astype(np.int8)
    metrics = np.column_stack([
        drop_top[sample_idx],
        up_top[sample_idx],
        drop_dump[sample_idx],
        up_dump[sample_idx],
        drop_eval[sample_idx],
        up_eval[sample_idx],
    ]).astype(np.float32)
    return x, y_top, y_dump, timestamps[sample_idx], metrics, names


def empty_samples(seq_len: int, features: int = 0) -> np.ndarray:
    return np.empty((0, seq_len, features), dtype=np.float32)


def make_features(bars: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    open_ = bars["open"].to_numpy(dtype=np.float64)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    qv = bars["quote_volume"].to_numpy(dtype=np.float64)
    taker_buy = bars["taker_buy_quote_volume"].to_numpy(dtype=np.float64)

    prev_close = pd.Series(close).shift(1).to_numpy()
    rng = np.maximum(high - low, 0.0)
    close_pos = safe_div(close - low, rng, 0.5)
    body = safe_div(close, open_, 1.0) - 1.0
    upper = safe_div(high - np.maximum(open_, close), close, 0.0)
    lower = safe_div(np.minimum(open_, close) - low, close, 0.0)
    taker_sell = 1.0 - safe_div(taker_buy, qv, 0.5)
    ret_close = safe_div(close, prev_close, 1.0) - 1.0
    vol20 = safe_div(qv, pd.Series(qv).shift(1).rolling(20, min_periods=20).mean().to_numpy(), 0.0)
    vol48 = safe_div(qv, pd.Series(qv).shift(1).rolling(48, min_periods=48).mean().to_numpy(), 0.0)
    ema8 = pd.Series(close).ewm(span=8, adjust=False).mean().to_numpy()
    ema21 = pd.Series(close).ewm(span=21, adjust=False).mean().to_numpy()
    dist_ema8 = safe_div(close, ema8, 1.0) - 1.0
    dist_ema21 = safe_div(close, ema21, 1.0) - 1.0
    ema_spread = safe_div(ema8, ema21, 1.0) - 1.0
    ret_std20 = pd.Series(ret_close).rolling(20, min_periods=20).std().to_numpy()
    atr14 = pd.Series(safe_div(high, low, 1.0) - 1.0).rolling(14, min_periods=14).mean().to_numpy()
    roll_high96 = pd.Series(high).rolling(96, min_periods=96).max().to_numpy()
    roll_low96 = pd.Series(low).rolling(96, min_periods=96).min().to_numpy()
    drawdown96 = safe_div(roll_high96, close, 1.0) - 1.0
    runup96 = safe_div(close, roll_low96, 1.0) - 1.0
    new_high96 = (high >= roll_high96).astype(float)

    cols = [
        ret_close,
        safe_div(high, low, 1.0) - 1.0,
        body,
        close_pos,
        upper,
        lower,
        np.log1p(np.maximum(qv, 0.0)),
        vol20,
        vol48,
        taker_sell,
        dist_ema8,
        dist_ema21,
        ema_spread,
        ret_std20,
        atr14,
        drawdown96,
        runup96,
        new_high96,
    ]
    names = [
        "ret_close",
        "range_pct",
        "body_pct",
        "close_pos",
        "upper_wick",
        "lower_wick",
        "log_quote_volume",
        "volume_ratio_20",
        "volume_ratio_48",
        "taker_sell_ratio",
        "dist_ema8",
        "dist_ema21",
        "ema8_ema21_spread",
        "ret_std20",
        "atr14",
        "drawdown_96",
        "runup_96",
        "is_new_high_96",
    ]

    optional = ["funding_rate", "oi", "oi_value", "top_pos_ratio", "global_acct_ratio", "taker_ratio"]
    for name in optional:
        if name in bars.columns:
            values = bars[name].astype(float).to_numpy()
            cols.append(values)
            names.append(name)
            if name in {"oi", "oi_value"}:
                pct_change = pd.Series(values).pct_change(4).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()
                cols.append(pct_change)
                names.append(f"{name}_chg_1h")

    out = np.column_stack(cols).astype(np.float32)
    out[~np.isfinite(out)] = np.nan
    return out, names


def future_min(values: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) <= horizon:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values[1:], horizon)
    out[: len(windows)] = windows.min(axis=1)
    return out


def future_max(values: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) <= horizon:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values[1:], horizon)
    out[: len(windows)] = windows.max(axis=1)
    return out


def safe_div(a: Any, b: Any, default: float) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(a, b)
    if np.isscalar(out):
        return np.array(default if not np.isfinite(out) else out)
    out = np.asarray(out, dtype=np.float64)
    out[~np.isfinite(out)] = default
    return out


if __name__ == "__main__":
    raise SystemExit(main())
