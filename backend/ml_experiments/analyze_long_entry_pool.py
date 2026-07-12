from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
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


DAY_MS = 86_400_000
BAR_MS = 900_000


@dataclass(frozen=True)
class RuleSpec:
    name: str
    rank30_max: int
    ret15_min: float | None
    ret30_min: float | None
    ret1h_min: float | None
    vol30_min: float
    heat24_max: float
    heat4h_max: float
    heat12h_max: float
    breakout: int
    close_pos_min: float
    upper_wick_max: float
    dist_ema21_max: float
    require_ema_stack: bool = True


NAMED_RULES = [
    RuleSpec("loose_fresh", 250, 0.015, 0.030, None, 2.0, 0.25, 0.15, 0.25, 8, 0.55, 0.08, 0.15, False),
    RuleSpec("balanced_fresh", 200, None, 0.040, 0.060, 2.5, 0.25, 0.18, 0.28, 16, 0.60, 0.06, 0.12, True),
    RuleSpec("strict_fresh", 150, None, 0.050, 0.070, 3.0, 0.20, 0.15, 0.22, 16, 0.68, 0.045, 0.10, True),
    RuleSpec("early_low_heat", 250, 0.020, 0.035, None, 2.5, 0.15, 0.12, 0.18, 32, 0.60, 0.06, 0.12, True),
    RuleSpec("volume_first", 120, 0.010, 0.025, None, 3.5, 0.20, 0.14, 0.22, 8, 0.55, 0.08, 0.14, False),
]


def main() -> int:
    args = parse_args()
    source = Path(args.source)
    settings = load_settings(args.config)
    symbols = discover_symbols(source, settings, args.symbols, args.max_symbols)
    frames: list[pd.DataFrame] = []
    skipped: list[str] = []
    for symbol in symbols:
        try:
            frame = build_symbol_frame(source / "klines" / f"{symbol}.parquet", symbol, args.days)
        except Exception as exc:
            skipped.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:120]}")
            continue
        if len(frame) == 0:
            skipped.append(f"{symbol}:no_rows")
            continue
        frames.append(frame)
        print(f"{symbol} rows={len(frame)}", flush=True)

    if not frames:
        raise SystemExit("no frames built")

    df = pd.concat(frames, ignore_index=True)
    df = add_cross_sectional_features(df)
    df = add_future_labels(df, args.horizon_bars)
    df = df.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    start_ts = int(df["timestamp"].min())
    end_ts = int(df["timestamp"].max())
    target_events = count_short_pool_events(df, args.target_lookback_bars)

    target_index = build_target_event_index(df, args.target_lookback_bars)
    named = [evaluate_rule(df, rule, args, target_events, target_index) for rule in NAMED_RULES]
    grid = [] if args.skip_grid else run_grid(df, args, target_events)
    payload = {
        "source": str(source),
        "symbols": len(frames),
        "rows": int(len(df)),
        "start_time": int(start_ts),
        "end_time": int(end_ts),
        "days": args.days,
        "horizon_bars": args.horizon_bars,
        "horizon_hours": args.horizon_bars / 4,
        "cooldown_bars": args.cooldown_bars,
        "target_events": int(target_events),
        "target_definition": "future entry into short pool: 4h>=20%, 12h>=30%, or 24h>=40%, while not already in short pool at entry",
        "named_rules": named,
        "grid_top": grid,
        "skipped_count": len(skipped),
        "skipped": skipped[:200],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(out.with_suffix(".md"), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate long-entry pool rules against later short-pool pump events.")
    parser.add_argument("--source", default=r"E:\A\币安数据库")
    parser.add_argument("--config", default="config/settings.json")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out", default="storage/ml/long_entry_pool_analysis.json")
    parser.add_argument("--horizon-bars", type=int, default=192, help="Future validation horizon. 192 15m bars = 48h.")
    parser.add_argument("--cooldown-bars", type=int, default=96, help="Deduplicate entries per symbol. 96 15m bars = 24h.")
    parser.add_argument("--target-lookback-bars", type=int, default=96, help="Short-pool target event reset window.")
    parser.add_argument("--min-events", type=int, default=80)
    parser.add_argument("--top-grid", type=int, default=20)
    parser.add_argument("--skip-grid", action="store_true")
    parser.add_argument("--grid-mode", choices=["compact", "wide"], default="compact")
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


def build_symbol_frame(path: Path, symbol: str, days: int) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    columns = ["timestamp", "open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume"]
    pf = pq.ParquetFile(path)
    max_ts = parquet_max_timestamp(pf)
    filters = None
    if days > 0 and max_ts is not None:
        filters = [("timestamp", ">=", int(max_ts) - days * DAY_MS + 1)]
    raw = pq.read_table(path, columns=columns, filters=filters).to_pandas()
    if raw.empty:
        return pd.DataFrame()
    raw = raw.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    raw["bucket"] = (raw["timestamp"].astype("int64") // BAR_MS) * BAR_MS
    bars = raw.groupby("bucket", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        quote_volume=("quote_volume", "sum"),
        taker_buy_quote_volume=("taker_buy_quote_volume", "sum"),
        minute_count=("timestamp", "count"),
    )
    bars = bars[bars["minute_count"] >= 10].reset_index().rename(columns={"bucket": "timestamp"})
    if len(bars) < 120:
        return pd.DataFrame()
    bars["symbol"] = symbol
    add_symbol_features(bars)
    return bars


def parquet_max_timestamp(pf: pq.ParquetFile) -> int | None:
    if "timestamp" not in pf.schema_arrow.names:
        return None
    idx = pf.schema_arrow.names.index("timestamp")
    values = []
    for i in range(pf.metadata.num_row_groups):
        stats = pf.metadata.row_group(i).column(idx).statistics
        if stats and stats.has_min_max:
            values.append(int(stats.max))
    return max(values) if values else None


def add_symbol_features(df: pd.DataFrame) -> None:
    close = df["close"]
    open_ = df["open"]
    high = df["high"]
    low = df["low"]
    qv = df["quote_volume"]
    body_high = pd.concat([open_, close], axis=1).max(axis=1)
    body_low = pd.concat([open_, close], axis=1).min(axis=1)
    rng = (high - low).replace(0.0, np.nan)
    df["body_high"] = body_high
    df["body_low"] = body_low
    df["ret15"] = close / close.shift(1) - 1.0
    df["ret30"] = close / close.shift(2) - 1.0
    df["ret1h"] = close / close.shift(4) - 1.0
    df["ret4h"] = close / close.shift(16) - 1.0
    df["ret12h"] = close / close.shift(48) - 1.0
    df["ret24h"] = close / close.shift(96) - 1.0
    df["qv30"] = qv.rolling(2, min_periods=2).sum()
    prev_qv30_mean = df["qv30"].shift(1).rolling(40, min_periods=20).mean()
    df["vol30_ratio"] = df["qv30"] / prev_qv30_mean
    prev_qv_mean = qv.shift(1).rolling(40, min_periods=20).mean()
    df["vol15_ratio"] = qv / prev_qv_mean
    ema8 = close.ewm(span=8, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    df["ema8"] = ema8
    df["ema21"] = ema21
    df["ema_stack"] = ema8 > ema21
    df["dist_ema21"] = close / ema21 - 1.0
    df["close_pos"] = ((close - low) / rng).fillna(0.5)
    df["upper_wick"] = ((high - body_high) / close).replace([np.inf, -np.inf], np.nan)
    df["body_pct"] = close / open_ - 1.0
    for lookback in (8, 16, 32):
        df[f"break_body_high_{lookback}"] = close > body_high.shift(1).rolling(lookback, min_periods=lookback).max()
    df["short_pool_now"] = (
        (df["ret4h"] >= 0.20)
        | (df["ret12h"] >= 0.30)
        | (df["ret24h"] >= 0.40)
    )
    df["already_hot_now"] = (
        (df["ret30"] >= 0.10)
        | (df["ret4h"] >= 0.20)
        | (df["ret12h"] >= 0.30)
        | (df["ret24h"] >= 0.40)
    )


def add_cross_sectional_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    df["qv30_rank"] = df.groupby("timestamp")["qv30"].rank(method="first", ascending=False)
    df["ret30_rank"] = df.groupby("timestamp")["ret30"].rank(method="first", ascending=False)
    return df


def add_future_labels(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    out = []
    for _symbol, g in df.groupby("symbol", sort=False):
        x = g.copy()
        close = x["close"].to_numpy(dtype=np.float64)
        high = x["high"].to_numpy(dtype=np.float64)
        low = x["low"].to_numpy(dtype=np.float64)
        short_now = x["short_pool_now"].fillna(False).to_numpy(dtype=bool)
        x["future_max_gain_48h"] = future_max(high, horizon) / close - 1.0
        x["future_max_close_gain_48h"] = future_max(close, horizon) / close - 1.0
        x["future_max_drawdown_48h"] = close / future_min(low, horizon) - 1.0
        fut_short = future_any(short_now, horizon)
        tts = time_to_future_true(short_now, horizon)
        x["future_short_pool_48h"] = fut_short
        x["bars_to_short_pool"] = tts
        out.append(x)
    return pd.concat(out, ignore_index=True)


def future_max(values: np.ndarray, horizon: int) -> np.ndarray:
    result = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) <= horizon:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(values[1:], horizon)
    result[: len(windows)] = windows.max(axis=1)
    return result


def future_min(values: np.ndarray, horizon: int) -> np.ndarray:
    result = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) <= horizon:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(values[1:], horizon)
    result[: len(windows)] = windows.min(axis=1)
    return result


def future_any(values: np.ndarray, horizon: int) -> np.ndarray:
    result = np.zeros(len(values), dtype=bool)
    if len(values) <= horizon:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(values[1:].astype(np.int8), horizon)
    result[: len(windows)] = windows.max(axis=1).astype(bool)
    return result


def time_to_future_true(values: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float32)
    true_idx = np.flatnonzero(values)
    for i in range(len(values)):
        pos = np.searchsorted(true_idx, i + 1)
        if pos < len(true_idx):
            delta = true_idx[pos] - i
            if 1 <= delta <= horizon:
                out[i] = delta
    return out


def build_target_event_index(df: pd.DataFrame, lookback: int) -> dict[str, np.ndarray]:
    index: dict[str, np.ndarray] = {}
    for _symbol, g in df.groupby("symbol", sort=False):
        s = g["short_pool_now"].fillna(False).to_numpy(dtype=bool)
        recent = pd.Series(s.astype(int)).shift(1).rolling(lookback, min_periods=1).max().fillna(0).to_numpy() > 0
        event_times = g.loc[s & ~recent, "timestamp"].to_numpy(dtype=np.int64)
        if len(event_times):
            index[str(g["symbol"].iloc[0])] = event_times
    return index


def count_short_pool_events(df: pd.DataFrame, lookback: int) -> int:
    return sum(len(v) for v in build_target_event_index(df, lookback).values())


def rule_mask(df: pd.DataFrame, rule: RuleSpec) -> np.ndarray:
    momentum_parts = []
    if rule.ret15_min is not None:
        momentum_parts.append(df["ret15"].to_numpy(dtype=np.float64) >= rule.ret15_min)
    if rule.ret30_min is not None:
        momentum_parts.append(df["ret30"].to_numpy(dtype=np.float64) >= rule.ret30_min)
    if rule.ret1h_min is not None:
        momentum_parts.append(df["ret1h"].to_numpy(dtype=np.float64) >= rule.ret1h_min)
    momentum = np.logical_or.reduce(momentum_parts) if momentum_parts else np.ones(len(df), dtype=bool)
    mask = (
        (df["qv30_rank"].to_numpy(dtype=np.float64) <= rule.rank30_max)
        & momentum
        & (df["vol30_ratio"].to_numpy(dtype=np.float64) >= rule.vol30_min)
        & (df["ret24h"].to_numpy(dtype=np.float64) <= rule.heat24_max)
        & (df["ret4h"].to_numpy(dtype=np.float64) <= rule.heat4h_max)
        & (df["ret12h"].to_numpy(dtype=np.float64) <= rule.heat12h_max)
        & df[f"break_body_high_{rule.breakout}"].fillna(False).to_numpy(dtype=bool)
        & (df["close_pos"].to_numpy(dtype=np.float64) >= rule.close_pos_min)
        & (df["upper_wick"].fillna(1.0).to_numpy(dtype=np.float64) <= rule.upper_wick_max)
        & (df["dist_ema21"].to_numpy(dtype=np.float64) <= rule.dist_ema21_max)
        & (df["body_pct"].to_numpy(dtype=np.float64) > 0.0)
        & ~df["already_hot_now"].fillna(False).to_numpy(dtype=bool)
    )
    if rule.require_ema_stack:
        mask &= df["ema_stack"].fillna(False).to_numpy(dtype=bool)
    return mask & np.isfinite(df["future_max_gain_48h"].to_numpy(dtype=np.float64))


def dedupe_by_symbol(df: pd.DataFrame, mask: np.ndarray, cooldown: int) -> pd.DataFrame:
    selected_positions: list[int] = []
    pos = 0
    for _symbol, g in df.groupby("symbol", sort=False):
        local = mask[pos : pos + len(g)]
        local_idx = np.flatnonzero(local)
        next_allowed = -1
        for idx in local_idx:
            if idx >= next_allowed:
                selected_positions.append(pos + int(idx))
                next_allowed = int(idx) + cooldown
        pos += len(g)
    return df.iloc[selected_positions].copy()


def evaluate_rule(
    df: pd.DataFrame,
    rule: RuleSpec,
    args: argparse.Namespace,
    target_events: int,
    target_index: dict[str, np.ndarray],
) -> dict[str, Any]:
    mask = rule_mask(df, rule)
    entries = dedupe_by_symbol(df, mask, args.cooldown_bars)
    return summarize_entries(entries, rule, args, target_events, target_index)


def summarize_entries(
    entries: pd.DataFrame,
    rule: RuleSpec,
    args: argparse.Namespace,
    target_events: int,
    target_index: dict[str, np.ndarray],
) -> dict[str, Any]:
    if entries.empty:
        return {"rule": asdict(rule), "events": 0}
    hit = entries["future_short_pool_48h"].fillna(False).to_numpy(dtype=bool)
    gain = entries["future_max_gain_48h"].to_numpy(dtype=np.float64)
    close_gain = entries["future_max_close_gain_48h"].to_numpy(dtype=np.float64)
    adverse = entries["future_max_drawdown_48h"].to_numpy(dtype=np.float64)
    lead = entries.loc[hit, "bars_to_short_pool"].to_numpy(dtype=np.float64) / 4.0
    heat24 = entries["ret24h"].to_numpy(dtype=np.float64)
    heat4h = entries["ret4h"].to_numpy(dtype=np.float64)
    events = int(len(entries))
    days = max((entries["timestamp"].max() - entries["timestamp"].min()) / DAY_MS, 1.0)
    captured, target_recall, lead_hours = target_event_recall(entries, target_index, args.horizon_bars)
    return {
        "name": rule.name,
        "rule": asdict(rule),
        "events": events,
        "hits": int(hit.sum()),
        "precision": safe_float(hit.mean()),
        "hit_per_target_event": safe_float(hit.sum() / target_events) if target_events else 0.0,
        "target_events_captured": int(captured),
        "target_recall": safe_float(target_recall),
        "events_per_day": safe_float(events / days),
        "median_future_high_gain": pct(gain, 50),
        "p75_future_high_gain": pct(gain, 75),
        "median_future_close_gain": pct(close_gain, 50),
        "median_adverse_drawdown": pct(adverse, 50),
        "p75_adverse_drawdown": pct(adverse, 75),
        "median_hours_to_short_pool": pct(lead, 50),
        "p75_hours_to_short_pool": pct(lead, 75),
        "median_lead_hours_to_target": pct(lead_hours, 50),
        "p75_lead_hours_to_target": pct(lead_hours, 75),
        "median_entry_ret24h": pct(heat24, 50),
        "p75_entry_ret24h": pct(heat24, 75),
        "median_entry_ret4h": pct(heat4h, 50),
        "p75_entry_ret4h": pct(heat4h, 75),
    }


def run_grid(df: pd.DataFrame, args: argparse.Namespace, target_events: int) -> list[dict[str, Any]]:
    target_index = build_target_event_index(df, args.target_lookback_bars)
    specs: list[RuleSpec] = []
    if args.grid_mode == "compact":
        ranks = (150, 200, 250)
        ret30s = (0.025, 0.035, 0.045)
        vols = (2.0, 2.5, 3.0)
        heats = (0.15, 0.25)
        breakouts = (8, 16)
    else:
        ranks = (120, 150, 200, 250)
        ret30s = (0.025, 0.035, 0.045, 0.055)
        vols = (2.0, 2.5, 3.0, 3.5)
        heats = (0.15, 0.25, 0.35)
        breakouts = (8, 16, 32)
    for rank in ranks:
        for ret30 in ret30s:
            for vol in vols:
                for heat24 in heats:
                    for breakout in breakouts:
                        specs.append(
                            RuleSpec(
                                name=f"grid_r{rank}_ret{ret30:.3f}_vol{vol:.1f}_h{heat24:.2f}_b{breakout}",
                                rank30_max=rank,
                                ret15_min=None,
                                ret30_min=ret30,
                                ret1h_min=None,
                                vol30_min=vol,
                                heat24_max=heat24,
                                heat4h_max=min(0.18, heat24),
                                heat12h_max=min(0.28, heat24 + 0.05),
                                breakout=breakout,
                                close_pos_min=0.60,
                                upper_wick_max=0.06,
                                dist_ema21_max=0.12,
                                require_ema_stack=True,
                            )
                        )
    rows = []
    for spec in specs:
        summary = evaluate_rule(df, spec, args, target_events, target_index)
        if summary.get("events", 0) < args.min_events:
            continue
        precision = float(summary.get("precision", 0.0))
        recall = float(summary.get("target_recall", 0.0))
        per_day = float(summary.get("events_per_day", 0.0))
        early_bonus = max(0.0, 0.25 - float(summary.get("median_entry_ret24h", 1.0)))
        activity_penalty = max(0.0, per_day - 8.0) * 0.01
        summary["score"] = precision * 0.55 + recall * 0.25 + early_bonus * 0.20 - activity_penalty
        rows.append(summary)
    rows.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return rows[: args.top_grid]


def target_event_recall(entries: pd.DataFrame, target_index: dict[str, np.ndarray], horizon_bars: int) -> tuple[int, float, np.ndarray]:
    if not target_index:
        return 0, 0.0, np.array([], dtype=np.float64)
    horizon_ms = horizon_bars * BAR_MS
    entry_index: dict[str, np.ndarray] = {}
    for symbol, g in entries.groupby("symbol", sort=False):
        entry_index[str(symbol)] = np.sort(g["timestamp"].to_numpy(dtype=np.int64))
    captured = 0
    leads: list[float] = []
    total = 0
    for symbol, target_times in target_index.items():
        total += len(target_times)
        entry_times = entry_index.get(symbol)
        if entry_times is None or len(entry_times) == 0:
            continue
        for target_ts in target_times:
            pos = np.searchsorted(entry_times, target_ts, side="left") - 1
            if pos >= 0:
                delta = int(target_ts) - int(entry_times[pos])
                if 0 < delta <= horizon_ms:
                    captured += 1
                    leads.append(delta / 3_600_000)
    return captured, captured / total if total else 0.0, np.asarray(leads, dtype=np.float64)


def pct(values: np.ndarray, q: float) -> float | None:
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return None
    return safe_float(np.percentile(values, q))


def safe_float(value: Any) -> float:
    try:
        x = float(value)
    except Exception:
        return 0.0
    return x if np.isfinite(x) else 0.0


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    def row(item: dict[str, Any]) -> str:
        return (
            f"| {item.get('name')} | {item.get('events')} | {item.get('hits')} | "
            f"{item.get('precision', 0):.1%} | {item.get('target_recall', 0):.1%} | "
            f"{item.get('events_per_day', 0):.1f} | {fmt_pct(item.get('median_entry_ret24h'))} | "
            f"{fmt_pct(item.get('median_future_high_gain'))} | {fmt_pct(item.get('median_adverse_drawdown'))} | "
            f"{fmt_hours(item.get('median_hours_to_short_pool'))} |"
        )

    lines = [
        "# Long Entry Pool Analysis",
        "",
        f"- symbols: {payload['symbols']}",
        f"- rows: {payload['rows']}",
        f"- target events: {payload['target_events']}",
        f"- horizon: {payload['horizon_hours']:.0f}h",
        "",
        "## Named Rules",
        "",
        "| rule | events | hits | precision | target recall | events/day | entry 24h | future high | adverse | hours to short pool |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(row(item) for item in payload["named_rules"])
    lines.extend(
        [
            "",
            "## Grid Top",
            "",
            "| rule | events | hits | precision | target recall | events/day | entry 24h | future high | adverse | hours to short pool |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(row(item) for item in payload["grid_top"][:10])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1%}"


def fmt_hours(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}"


if __name__ == "__main__":
    raise SystemExit(main())
