"""Discover 1m waterfall short patterns across all local Binance futures data.

This is an exploratory, offline script. It does not touch production strategy
code. The goal is to find where tradable waterfall drops actually come from:
post-pump, range breakdown, downtrend continuation, or other states.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

try:
    from pump_dump_hunter.ml.train import EXCLUDE
except Exception:
    EXCLUDE = {
        "BTCUSDT",
        "ETHUSDT",
        "BNBUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "TRXUSDT",
        "XAUUSDT",
        "XAGUSDT",
        "XAUTUSDT",
        "PAXGUSDT",
        "QQQUSDT",
        "SPXUSDT",
        "SPYUSDT",
        "AAPLUSDT",
        "AMZNUSDT",
        "AMDUSDT",
        "COINUSDT",
        "CRCLUSDT",
        "EWYUSDT",
        "GOOGUSDT",
        "INTCUSDT",
        "METAUSDT",
        "MSTRUSDT",
        "MSFTUSDT",
        "MUUSDT",
        "NFLXUSDT",
        "NVDAUSDT",
        "SNDKUSDT",
        "TSLAUSDT",
    }


DAY_MS = 86_400_000
FEE_ROUND_TRIP = 0.0008


@dataclass(frozen=True)
class SignalRule:
    name: str
    min_qv30: float
    min_body_drop: float
    min_2m_drop: float
    min_5m_drop: float
    min_volr20: float
    min_volr5_20: float
    min_tsell: float
    max_close_pos: float
    min_upper_wick: float
    break_lookback: int
    break_buffer: float
    min_prior_context: str = "any"
    min_ret_30m: float = -9.0
    max_ret_30m: float = 9.0
    min_ret_2h: float = -9.0
    max_ret_2h: float = 9.0
    min_ret_4h: float = -9.0
    max_ret_4h: float = 9.0
    min_ret_12h: float = -9.0
    max_ret_12h: float = 9.0
    min_ret_24h: float = -9.0
    max_ret_24h: float = 9.0
    min_drop_5m_entry: float = 0.0
    min_runup_24h: float = -9.0
    max_runup_24h: float = 9.0
    min_dd_from_24h_high: float = 0.0
    max_dd_from_24h_high: float = 9.0
    min_qv_over_prev6max: float = 0.0
    max_qv_over_prev6max: float = 999.0
    min_red_streak: int = 0
    min_lower_wick: float = 0.0
    max_lower_wick: float = 9.0
    min_range_pct: float = 0.0
    max_range_pct: float = 9.0
    min_break_depth: float = 0.0
    max_volr20: float = 999.0
    max_volr5_20: float = 999.0
    stop_cap: float = 0.035
    stop_body_high_buffer: float = 0.004
    trail_activate: float = 0.035
    trail_rebound: float = 0.010
    quick_reclaim_buffer: float = 0.004
    rebound_activate: float = 0.025
    rebound_retrace: float = 0.022
    max_hold_min: int = 180


@dataclass
class WaterfallEvent:
    symbol: str
    event_time: int
    event_price: float
    future_drop_15m: float
    future_drop_30m: float
    future_drop_60m: float
    adverse_5m: float
    adverse_15m: float
    t_to_low_min: int
    ret_30m: float
    ret_2h: float
    ret_4h: float
    ret_12h: float
    ret_24h: float
    runup_24h: float
    dd_from_24h_high: float
    qv30: float
    volr20: float
    volr5_20: float
    tsell: float
    tsell5: float
    body_drop: float
    drop_2m: float
    drop_5m: float
    close_pos: float
    upper_wick: float
    lower_wick: float
    range_pct: float
    break_depth: float
    ema8_dist: float
    ema21_dist: float
    ema8_21: float
    atr20: float
    red_streak: int
    qv_over_prev6max: float
    family: str


@dataclass
class Trade:
    symbol: str
    rule: str
    family: str
    signal_time: int
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    ret: float
    mae: float
    mfe: float
    hold_min: int
    exit_reason: str
    ret_30m: float
    ret_2h: float
    ret_4h: float
    ret_12h: float
    ret_24h: float
    runup_24h: float
    dd_from_24h_high: float
    qv30: float
    volr20: float
    volr5_20: float
    tsell: float
    tsell5: float
    body_drop: float
    drop_2m: float
    drop_5m: float
    close_pos: float
    upper_wick: float
    lower_wick: float
    range_pct: float
    break_depth: float
    red_streak: int
    qv_over_prev6max: float


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.source)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = parquet_files(source, args.max_symbols)
    print(json.dumps({"files": len(files), "source": str(source)}, ensure_ascii=False), flush=True)

    all_events: list[WaterfallEvent] = []
    all_trades: list[Trade] = []
    rules = build_rules()
    if args.rule_include:
        include = {x.strip() for x in args.rule_include.split(",") if x.strip()}
        rules = [rule for rule in rules if rule.name in include]
    for idx, path in enumerate(files, 1):
        try:
            df = load_symbol(path, args.days)
        except Exception as exc:
            print(f"skip {path.stem}: {exc}", flush=True)
            continue
        if len(df) < 3000:
            continue
        events = discover_events(path.stem.upper(), df)
        all_events.extend(events)
        for rule in rules:
            all_trades.extend(backtest_rule(path.stem.upper(), df, rule))
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed {idx}/{len(files)} events={len(all_events)} trades={len(all_trades)}", flush=True)

    events_df = pd.DataFrame([asdict(x) for x in all_events])
    trades_df = pd.DataFrame([asdict(x) for x in all_trades])
    if not events_df.empty:
        events_df["event_time_iso"] = events_df["event_time"].map(iso_ms)
    if not trades_df.empty:
        trades_df["signal_time_iso"] = trades_df["signal_time"].map(iso_ms)
        trades_df["entry_time_iso"] = trades_df["entry_time"].map(iso_ms)
        trades_df["exit_time_iso"] = trades_df["exit_time"].map(iso_ms)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    events_path = out_dir / f"waterfall_events_{stamp}.csv"
    trades_path = out_dir / f"waterfall_trades_{stamp}.csv"
    summary_path = out_dir / f"waterfall_summary_{stamp}.json"
    report_path = out_dir / f"waterfall_report_{stamp}.md"
    events_df.to_csv(events_path, index=False)
    trades_df.to_csv(trades_path, index=False)
    summary = summarize(events_df, trades_df)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"events": str(events_path), "trades": str(trades_path), "summary": str(summary_path), "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=os.environ.get("HUNTER_BB_SOURCE", r"E:\A\bb\data"))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--out-dir", default="backend/storage/ml/waterfall_patterns")
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--rule-include", default="")
    return parser.parse_args(argv)


def parquet_files(source: Path, max_symbols: int) -> list[Path]:
    root = source / "klines"
    files = []
    for name in glob.glob(str(root / "*.parquet")):
        path = Path(name)
        sym = path.stem.upper()
        if sym in EXCLUDE or path.name.endswith(".bak"):
            continue
        files.append(path)
    files.sort(key=lambda p: p.stem.upper())
    if max_symbols > 0:
        files = files[:max_symbols]
    return files


def load_symbol(path: Path, days: int) -> pd.DataFrame:
    table = pq.read_table(path)
    df = table.to_pandas()
    if df.empty:
        return df
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    if days > 0:
        end = int(df["timestamp"].max())
        start = end - days * DAY_MS
        df = df[df["timestamp"] >= start].copy().reset_index(drop=True)
    df = df.rename(
        columns={
            "timestamp": "t",
            "quote_volume": "qv",
            "taker_buy_quote_volume": "tbq",
        }
    )
    return add_features(df)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    o = out["open"].astype(float)
    h = out["high"].astype(float)
    l = out["low"].astype(float)
    c = out["close"].astype(float)
    qv = out["qv"].astype(float)
    body_low = np.minimum(o, c)
    body_high = np.maximum(o, c)
    rng = (h - l).replace(0, np.nan)
    out["body_low"] = body_low
    out["body_high"] = body_high
    out["close_pos"] = (c - l) / rng
    out["body_drop"] = (o - c) / o
    out["upper_wick"] = (h - body_high) / o
    out["lower_wick"] = (body_low - l) / o
    out["range_pct"] = (h - l) / o
    out["drop_2m"] = 1.0 - c / c.shift(2)
    out["drop_5m"] = 1.0 - c / c.shift(5)
    out["ret_30m"] = c / c.shift(30) - 1.0
    out["ret_2h"] = c / c.shift(120) - 1.0
    out["ret_4h"] = c / c.shift(240) - 1.0
    out["ret_12h"] = c / c.shift(720) - 1.0
    out["ret_24h"] = c / c.shift(1440) - 1.0
    out["runup_24h"] = c / l.rolling(1440).min() - 1.0
    out["dd_from_24h_high"] = 1.0 - c / h.rolling(1440).max()
    out["qv30"] = qv.rolling(30).sum()
    out["volr20"] = qv / qv.rolling(20).mean()
    out["volr5_20"] = qv.rolling(5).sum() / qv.rolling(20).mean().rolling(5).sum()
    out["tsell"] = 1.0 - out["tbq"].astype(float) / qv.replace(0, np.nan)
    out["tsell5"] = 1.0 - out["tbq"].astype(float).rolling(5).sum() / qv.rolling(5).sum().replace(0, np.nan)
    out["ema8"] = c.ewm(span=8, adjust=False).mean()
    out["ema21"] = c.ewm(span=21, adjust=False).mean()
    out["ema8_dist"] = c / out["ema8"] - 1.0
    out["ema21_dist"] = c / out["ema21"] - 1.0
    out["ema8_21"] = out["ema8"] / out["ema21"] - 1.0
    out["atr20"] = ((h - l) / c).rolling(20).mean()
    out["qv_over_prev6max"] = qv / qv.shift(1).rolling(6).max()
    red = c < o
    out["red_streak_now"] = red.astype(int).groupby((~red).cumsum()).cumsum()
    for n in (8, 20, 40):
        out[f"prior_body_low_{n}"] = body_low.rolling(n).min().shift(1)
    return out


def discover_events(symbol: str, df: pd.DataFrame) -> list[WaterfallEvent]:
    events: list[WaterfallEvent] = []
    n = len(df)
    if n < 1600:
        return events
    low = df["low"].astype(float)
    high = df["high"].astype(float)
    close = df["close"].astype(float)
    future_low_15 = low.shift(-1).rolling(15).min().shift(-14)
    future_low_30 = low.shift(-1).rolling(30).min().shift(-29)
    future_low_60 = low.shift(-1).rolling(60).min().shift(-59)
    future_high_5 = high.shift(-1).rolling(5).max().shift(-4)
    future_high_15 = high.shift(-1).rolling(15).max().shift(-14)
    drop_30 = close / future_low_30 - 1.0
    adverse_5 = future_high_5 / close - 1.0
    base = (
        (drop_30 >= 0.035)
        & (adverse_5 <= 0.018)
        & df["qv30"].notna()
        & (df["qv30"] >= 50_000)
    ).fillna(False)
    idx = np.flatnonzero(base.to_numpy())
    if len(idx) == 0:
        return events
    cooldown_until = -1
    for i in idx:
        if i < 1500 or i >= n - 65:
            continue
        if i < cooldown_until:
            continue
        future = df.iloc[i + 1: i + 61]
        low_pos = int(np.argmin(future["low"].to_numpy())) + 1
        events.append(
            event_from_row(
                symbol,
                df,
                int(i),
                float(close.iloc[i] / future_low_15.iloc[i] - 1.0),
                float(drop_30.iloc[i]),
                float(close.iloc[i] / future_low_60.iloc[i] - 1.0),
                float(adverse_5.iloc[i]),
                float(future_high_15.iloc[i] / close.iloc[i] - 1.0),
                low_pos,
            )
        )
        cooldown_until = int(i) + 30
    return events


def event_from_row(
    symbol: str,
    df: pd.DataFrame,
    i: int,
    drop_15: float,
    drop_30: float,
    drop_60: float,
    adverse_5: float,
    adverse_15: float,
    low_pos: int,
) -> WaterfallEvent:
    row = df.iloc[i]
    close = float(row["close"])
    prior_low = float(row["prior_body_low_20"]) if np.isfinite(row["prior_body_low_20"]) else close
    return WaterfallEvent(
        symbol=symbol,
        event_time=int(row["t"]),
        event_price=close,
        future_drop_15m=drop_15,
        future_drop_30m=drop_30,
        future_drop_60m=drop_60,
        adverse_5m=adverse_5,
        adverse_15m=adverse_15,
        t_to_low_min=low_pos,
        ret_30m=float(row["ret_30m"]),
        ret_2h=float(row["ret_2h"]),
        ret_4h=float(row["ret_4h"]),
        ret_12h=float(row["ret_12h"]),
        ret_24h=float(row["ret_24h"]),
        runup_24h=float(row["runup_24h"]),
        dd_from_24h_high=float(row["dd_from_24h_high"]),
        qv30=float(row["qv30"]),
        volr20=float(row["volr20"]),
        volr5_20=float(row["volr5_20"]),
        tsell=float(row["tsell"]),
        tsell5=float(row["tsell5"]),
        body_drop=float(row["body_drop"]),
        drop_2m=float(row["drop_2m"]),
        drop_5m=float(row["drop_5m"]),
        close_pos=float(row["close_pos"]),
        upper_wick=float(row["upper_wick"]),
        lower_wick=float(row["lower_wick"]),
        range_pct=float(row["range_pct"]),
        break_depth=prior_low / close - 1.0,
        ema8_dist=float(row["ema8_dist"]),
        ema21_dist=float(row["ema21_dist"]),
        ema8_21=float(row["ema8_21"]),
        atr20=float(row["atr20"]),
        red_streak=red_streak(df, i),
        qv_over_prev6max=float(row["qv_over_prev6max"]),
        family=classify_family(row),
    )


def classify_family(row: pd.Series) -> str:
    ret24 = float(row["ret_24h"])
    ret4 = float(row["ret_4h"])
    ret30 = float(row["ret_30m"])
    run24 = float(row["runup_24h"])
    dd = float(row["dd_from_24h_high"])
    if ret24 >= 0.28 or run24 >= 0.45:
        return "post_pump"
    if ret4 <= -0.08 and ret30 <= -0.015:
        return "downtrend_continuation"
    if abs(ret4) <= 0.06 and dd <= 0.18:
        return "range_breakdown"
    if ret30 <= -0.04:
        return "momentum_dump"
    return "other"


def build_rules() -> list[SignalRule]:
    return [
        SignalRule("all_1m_break", 80_000, 0.006, 0.010, 0.018, 2.2, 1.7, 0.52, 0.42, 0.0, 20, 0.001),
        SignalRule("wick_reject_1m", 80_000, 0.006, 0.010, 0.018, 2.2, 1.7, 0.52, 0.42, 0.0015, 20, 0.001),
        SignalRule("strong_sell_1m", 80_000, 0.008, 0.014, 0.022, 2.8, 2.0, 0.56, 0.35, 0.0015, 20, 0.001),
        SignalRule("range_break_1m", 80_000, 0.006, 0.010, 0.018, 2.0, 1.6, 0.52, 0.42, 0.0010, 40, 0.001, "not_post_pump"),
        SignalRule("post_pump_1m", 80_000, 0.006, 0.010, 0.018, 2.0, 1.6, 0.52, 0.42, 0.0010, 20, 0.001, "post_pump"),
        SignalRule("downtrend_waterfall_1m", 80_000, 0.008, 0.014, 0.030, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_2h=-0.13),
        SignalRule("downtrend_waterfall_tight25_1m", 80_000, 0.008, 0.014, 0.030, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_2h=-0.13, stop_cap=0.025, trail_activate=0.025, trail_rebound=0.008, quick_reclaim_buffer=0.0025, rebound_activate=0.020, rebound_retrace=0.016),
        SignalRule("downtrend_waterfall_tight20_1m", 80_000, 0.008, 0.014, 0.030, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_2h=-0.13, stop_cap=0.020, trail_activate=0.025, trail_rebound=0.008, quick_reclaim_buffer=0.0020, rebound_activate=0.020, rebound_retrace=0.014),
        SignalRule("deep_downtrend_waterfall_1m", 80_000, 0.008, 0.014, 0.055, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_2h=-0.15, min_drop_5m_entry=0.055),
        SignalRule("deep_downtrend_tight25_1m", 80_000, 0.008, 0.014, 0.055, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_2h=-0.15, min_drop_5m_entry=0.055, stop_cap=0.025, trail_activate=0.025, trail_rebound=0.008, quick_reclaim_buffer=0.0025, rebound_activate=0.020, rebound_retrace=0.016),
        SignalRule("fast_dump_continuation_1m", 80_000, 0.008, 0.014, 0.050, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "any", max_ret_30m=-0.09, max_ret_2h=-0.12, min_drop_5m_entry=0.050),
        SignalRule("post_pump_near_high_reject_1m", 300_000, 0.006, 0.010, 0.018, 2.0, 1.6, 0.52, 0.42, 0.0028, 20, 0.001, "post_pump", max_dd_from_24h_high=0.13, min_qv_over_prev6max=1.35, max_range_pct=0.075),
        SignalRule("post_pump_strong_sell_control_1m", 200_000, 0.008, 0.014, 0.022, 2.8, 2.0, 0.62, 0.35, 0.0015, 20, 0.001, "post_pump", min_ret_12h=0.0, max_volr20=4.5),
        SignalRule("range_low_runup_break_1m", 80_000, 0.006, 0.010, 0.018, 2.0, 1.6, 0.52, 0.42, 0.0010, 40, 0.001, "range_breakdown", min_ret_2h=-0.045, max_runup_24h=0.003, min_drop_5m_entry=0.018, min_red_streak=2),
        SignalRule("range_low_runup_reject_1m", 300_000, 0.006, 0.010, 0.016, 2.2, 1.8, 0.52, 0.42, 0.0015, 20, 0.001, "range_breakdown", max_ret_12h=-0.04, max_runup_24h=0.003, max_range_pct=0.018),
        SignalRule("range_low_runup_strong_sell_1m", 200_000, 0.008, 0.014, 0.014, 2.8, 2.0, 0.56, 0.35, 0.0010, 20, 0.001, "range_breakdown", min_ret_2h=-0.045, max_runup_24h=0.003, min_red_streak=2, max_range_pct=0.028),
        SignalRule("downtrend_balanced_break_1m", 80_000, 0.006, 0.010, 0.018, 2.0, 1.6, 0.52, 0.42, 0.0010, 40, 0.001, "downtrend", max_ret_30m=-0.11, max_volr20=6.2),
        SignalRule("downtrend_deep_strong_sell_1m", 80_000, 0.008, 0.014, 0.022, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_2h=-0.17, max_ret_24h=-0.14, max_volr20=6.2),
        SignalRule("momentum_early_waterfall_1m", 80_000, 0.008, 0.014, 0.018, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "momentum_dump", min_ret_2h=-0.075, min_lower_wick=0.005, max_range_pct=0.065),
        SignalRule("other_downshift_reject_1m", 80_000, 0.006, 0.010, 0.018, 2.2, 1.7, 0.52, 0.42, 0.0015, 20, 0.001, "other", max_ret_12h=-0.09, max_runup_24h=0.075, min_drop_5m_entry=0.017),
        SignalRule("robust_post_pump_strong_sell_1m", 80_000, 0.008, 0.014, 0.022, 2.8, 2.0, 0.595, 0.35, 0.0015, 20, 0.001, "post_pump", min_ret_24h=0.385, min_ret_30m=-0.071),
        SignalRule("robust_post_pump_red_sell_1m", 80_000, 0.008, 0.014, 0.022, 2.8, 1.6, 0.595, 0.35, 0.0015, 20, 0.001, "post_pump", min_ret_24h=0.385, min_red_streak=2),
        SignalRule("robust_downtrend_range_flush_1m", 80_000, 0.008, 0.014, 0.022, 2.8, 2.0, 0.56, 0.36, 0.0015, 20, 0.001, "downtrend", max_ret_4h=-0.17, min_range_pct=0.052, max_volr5_20=2.94),
        SignalRule("robust_downtrend_upper_break_1m", 80_000, 0.006, 0.010, 0.018, 2.2, 1.7, 0.52, 0.42, 0.0043, 20, 0.001, "downtrend", max_ret_2h=-0.176, max_volr5_20=2.7),
        SignalRule("robust_momentum_uptrend_dump_1m", 80_000, 0.006, 0.010, 0.018, 2.2, 1.7, 0.52, 0.42, 0.0, 20, 0.001, "momentum_dump", min_ret_2h=0.064, min_ret_12h=0.010, max_qv_over_prev6max=3.05),
        SignalRule("robust_momentum_lower_wick_dump_1m", 80_000, 0.006, 0.010, 0.018, 2.2, 1.7, 0.52, 0.42, 0.0, 20, 0.001, "momentum_dump", min_ret_2h=0.064, min_lower_wick=0.002, max_qv_over_prev6max=3.05),
        SignalRule("robust_other_pullback_dump_1m", 80_000, 0.008, 0.014, 0.022, 2.8, 2.0, 0.56, 0.35, 0.0015, 20, 0.001, "other", min_dd_from_24h_high=0.094, min_lower_wick=0.006, min_ret_4h=-0.029),
    ]


def backtest_rule(symbol: str, df: pd.DataFrame, rule: SignalRule) -> list[Trade]:
    trades: list[Trade] = []
    cooldown_until = -1
    n = len(df)
    idx = candidate_indices(df, rule)
    for i in idx:
        if i < 1500 or i >= n - 90:
            continue
        if i < cooldown_until:
            continue
        if signal_ok(df, i, rule):
            trade = simulate_trade(symbol, df, i, rule)
            if trade:
                trades.append(trade)
                cooldown_until = i + max(15, trade.hold_min + 15)
    return trades


def candidate_indices(df: pd.DataFrame, rule: SignalRule) -> np.ndarray:
    close = df["close"].astype(float)
    prior_low = df[f"prior_body_low_{rule.break_lookback}"].astype(float)
    body_break = close < prior_low * (1.0 - rule.break_buffer)
    one_bar = (df["body_drop"] >= rule.min_body_drop) & (df["volr20"] >= rule.min_volr20)
    two_bar = (df["drop_2m"] >= rule.min_2m_drop) & (df["volr20"] >= rule.min_volr20)
    five_bar = (df["drop_5m"] >= rule.min_5m_drop) & (df["volr5_20"] >= rule.min_volr5_20)
    mask = (
        body_break
        & (one_bar | two_bar | five_bar)
        & (df["qv30"] >= rule.min_qv30)
        & (df["volr20"] <= rule.max_volr20)
        & (df["volr5_20"] <= rule.max_volr5_20)
        & (df["close_pos"] <= rule.max_close_pos)
        & (df["tsell"] >= rule.min_tsell)
        & (df["upper_wick"] >= rule.min_upper_wick)
        & (df["lower_wick"] >= rule.min_lower_wick)
        & (df["lower_wick"] <= rule.max_lower_wick)
        & (df["range_pct"] >= rule.min_range_pct)
        & (df["range_pct"] <= rule.max_range_pct)
        & (df["ret_30m"] >= rule.min_ret_30m)
        & (df["ret_30m"] <= rule.max_ret_30m)
        & (df["ret_2h"] >= rule.min_ret_2h)
        & (df["ret_2h"] <= rule.max_ret_2h)
        & (df["ret_4h"] >= rule.min_ret_4h)
        & (df["ret_4h"] <= rule.max_ret_4h)
        & (df["ret_12h"] >= rule.min_ret_12h)
        & (df["ret_12h"] <= rule.max_ret_12h)
        & (df["ret_24h"] >= rule.min_ret_24h)
        & (df["ret_24h"] <= rule.max_ret_24h)
        & (df["runup_24h"] >= rule.min_runup_24h)
        & (df["runup_24h"] <= rule.max_runup_24h)
        & (df["dd_from_24h_high"] >= rule.min_dd_from_24h_high)
        & (df["dd_from_24h_high"] <= rule.max_dd_from_24h_high)
        & (df["qv_over_prev6max"] >= rule.min_qv_over_prev6max)
        & (df["qv_over_prev6max"] <= rule.max_qv_over_prev6max)
        & (df["red_streak_now"] >= rule.min_red_streak)
    ).fillna(False)
    return np.flatnonzero(mask.to_numpy())


def signal_ok(df: pd.DataFrame, i: int, rule: SignalRule) -> bool:
    row = df.iloc[i]
    close = float(row["close"])
    if not np.isfinite(close) or close <= 0:
        return False
    if float(row["qv30"]) < rule.min_qv30:
        return False
    if float(row["close_pos"]) > rule.max_close_pos:
        return False
    if float(row["tsell"]) < rule.min_tsell:
        return False
    if float(row["upper_wick"]) < rule.min_upper_wick:
        return False
    if not (rule.min_lower_wick <= float(row["lower_wick"]) <= rule.max_lower_wick):
        return False
    if not (rule.min_range_pct <= float(row["range_pct"]) <= rule.max_range_pct):
        return False
    if context_blocked(row, rule.min_prior_context):
        return False
    if not (rule.min_ret_30m <= float(row["ret_30m"]) <= rule.max_ret_30m):
        return False
    if not (rule.min_ret_2h <= float(row["ret_2h"]) <= rule.max_ret_2h):
        return False
    if not (rule.min_ret_4h <= float(row["ret_4h"]) <= rule.max_ret_4h):
        return False
    if not (rule.min_ret_12h <= float(row["ret_12h"]) <= rule.max_ret_12h):
        return False
    if not (rule.min_ret_24h <= float(row["ret_24h"]) <= rule.max_ret_24h):
        return False
    if float(row["drop_5m"]) < rule.min_drop_5m_entry:
        return False
    if not (rule.min_runup_24h <= float(row["runup_24h"]) <= rule.max_runup_24h):
        return False
    if not (rule.min_dd_from_24h_high <= float(row["dd_from_24h_high"]) <= rule.max_dd_from_24h_high):
        return False
    if not (rule.min_qv_over_prev6max <= float(row["qv_over_prev6max"]) <= rule.max_qv_over_prev6max):
        return False
    if int(row["red_streak_now"]) < rule.min_red_streak:
        return False
    prior_low = float(row[f"prior_body_low_{rule.break_lookback}"])
    if not np.isfinite(prior_low) or close >= prior_low * (1.0 - rule.break_buffer):
        return False
    if prior_low / close - 1.0 < rule.min_break_depth:
        return False
    if float(row["volr20"]) > rule.max_volr20 or float(row["volr5_20"]) > rule.max_volr5_20:
        return False
    one_bar = float(row["body_drop"]) >= rule.min_body_drop and float(row["volr20"]) >= rule.min_volr20
    two_bar = float(row["drop_2m"]) >= rule.min_2m_drop and float(row["volr20"]) >= rule.min_volr20
    five_bar = float(row["drop_5m"]) >= rule.min_5m_drop and float(row["volr5_20"]) >= rule.min_volr5_20
    return bool(one_bar or two_bar or five_bar)


def context_blocked(row: pd.Series, mode: str) -> bool:
    family = classify_family(row)
    if mode == "post_pump":
        return family != "post_pump"
    if mode == "downtrend":
        return family != "downtrend_continuation"
    if mode in {"range_breakdown", "momentum_dump", "other"}:
        return family != mode
    if mode == "not_post_pump":
        return family == "post_pump"
    return False


def simulate_trade(symbol: str, df: pd.DataFrame, signal_i: int, rule: SignalRule) -> Trade | None:
    entry_i = signal_i + 1
    if entry_i >= len(df):
        return None
    sig = df.iloc[signal_i]
    entry_row = df.iloc[entry_i]
    entry = float(entry_row["open"])
    if not np.isfinite(entry) or entry <= 0:
        return None
    recent_high = float(df.iloc[max(0, signal_i - rule.break_lookback): signal_i + 1]["body_high"].max())
    stop = min(max(float(sig["high"]), recent_high) * (1.0 + rule.stop_body_high_buffer), entry * (1.0 + rule.stop_cap))
    best_low = entry
    worst_high = entry
    exit_i = min(entry_i + rule.max_hold_min, len(df) - 1)
    exit_price = float(df.iloc[exit_i]["close"])
    reason = "timeout"
    trailing_active = False
    trailing_low = entry
    trail = 0.0
    for j in range(entry_i, min(len(df), entry_i + rule.max_hold_min + 1)):
        row = df.iloc[j]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        worst_high = max(worst_high, high)
        if high >= stop:
            exit_i = j
            exit_price = stop
            reason = "stop"
            break
        mfe = entry / best_low - 1.0
        if trailing_active:
            if high >= trail:
                exit_i = j
                exit_price = trail
                reason = "trailing_stop"
                break
        if j <= entry_i + 3:
            prior_low = float(sig[f"prior_body_low_{rule.break_lookback}"])
            if close > prior_low * (1.0 + rule.quick_reclaim_buffer):
                exit_i = j
                exit_price = close
                reason = "quick_reclaim"
                break
        if mfe >= rule.rebound_activate:
            rebound = close / best_low - 1.0
            if rebound >= rule.rebound_retrace:
                exit_i = j
                exit_price = close
                reason = "rebound_trail"
                break
        best_low = min(best_low, low)
        mfe = entry / best_low - 1.0
        if not trailing_active and mfe >= rule.trail_activate:
            trailing_active = True
            trailing_low = best_low
            trail = trailing_low * (1.0 + rule.trail_rebound)
        elif trailing_active:
            trailing_low = min(trailing_low, best_low)
            trail = min(trail, trailing_low * (1.0 + rule.trail_rebound))
    ret = 1.0 - exit_price / entry - FEE_ROUND_TRIP
    return Trade(
        symbol=symbol,
        rule=rule.name,
        family=classify_family(sig),
        signal_time=int(sig["t"]),
        entry_time=int(entry_row["t"]),
        exit_time=int(df.iloc[exit_i]["t"]),
        entry=entry,
        exit=exit_price,
        ret=ret,
        mae=worst_high / entry - 1.0,
        mfe=entry / best_low - 1.0,
        hold_min=max(1, exit_i - entry_i + 1),
        exit_reason=reason,
        ret_30m=float(sig["ret_30m"]),
        ret_2h=float(sig["ret_2h"]),
        ret_4h=float(sig["ret_4h"]),
        ret_12h=float(sig["ret_12h"]),
        ret_24h=float(sig["ret_24h"]),
        runup_24h=float(sig["runup_24h"]),
        dd_from_24h_high=float(sig["dd_from_24h_high"]),
        qv30=float(sig["qv30"]),
        volr20=float(sig["volr20"]),
        volr5_20=float(sig["volr5_20"]),
        tsell=float(sig["tsell"]),
        tsell5=float(sig["tsell5"]),
        body_drop=float(sig["body_drop"]),
        drop_2m=float(sig["drop_2m"]),
        drop_5m=float(sig["drop_5m"]),
        close_pos=float(sig["close_pos"]),
        upper_wick=float(sig["upper_wick"]),
        lower_wick=float(sig["lower_wick"]),
        range_pct=float(sig["range_pct"]),
        break_depth=float(sig[f"prior_body_low_{rule.break_lookback}"]) / float(sig["close"]) - 1.0,
        red_streak=red_streak(df, signal_i),
        qv_over_prev6max=float(sig["qv_over_prev6max"]),
    )


def red_streak(df: pd.DataFrame, i: int) -> int:
    streak = 0
    for pos in range(i, max(-1, i - 20), -1):
        row = df.iloc[pos]
        if float(row["close"]) < float(row["open"]):
            streak += 1
        else:
            break
    return streak


def summarize(events: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {
        "events": {},
        "trades": {},
    }
    if not events.empty:
        out["events"]["total"] = int(len(events))
        out["events"]["symbols"] = int(events["symbol"].nunique())
        out["events"]["by_family"] = summarize_group(events, "family")
    if not trades.empty:
        out["trades"]["total"] = int(len(trades))
        out["trades"]["symbols"] = int(trades["symbol"].nunique())
        out["trades"]["by_rule"] = summarize_trades(trades, "rule")
        out["trades"]["by_rule_family"] = {
            f"{rule}/{fam}": metrics(g)
            for (rule, fam), g in trades.groupby(["rule", "family"])
        }
        out["trades"]["by_exit_reason"] = {
            f"{rule}/{reason}": metrics(g)
            for (rule, reason), g in trades.groupby(["rule", "exit_reason"])
        }
    return out


def summarize_group(df: pd.DataFrame, col: str) -> dict[str, Any]:
    return {
        str(k): {
            "events": int(len(g)),
            "symbols": int(g["symbol"].nunique()),
            "median_drop_30m": pct(g["future_drop_30m"].median()),
            "median_adverse_5m": pct(g["adverse_5m"].median()),
            "median_ret_24h": pct(g["ret_24h"].median()),
            "median_runup_24h": pct(g["runup_24h"].median()),
        }
        for k, g in df.groupby(col)
    }


def summarize_trades(df: pd.DataFrame, col: str) -> dict[str, Any]:
    return {str(k): metrics(g) for k, g in df.groupby(col)}


def metrics(g: pd.DataFrame) -> dict[str, Any]:
    pos = g.loc[g["ret"] > 0, "ret"].sum()
    neg = -g.loc[g["ret"] < 0, "ret"].sum()
    return {
        "trades": int(len(g)),
        "symbols": int(g["symbol"].nunique()),
        "win_rate": pct((g["ret"] > 0).mean()),
        "avg_ret": pct(g["ret"].mean()),
        "median_ret": pct(g["ret"].median()),
        "profit_factor": round(float(pos / neg), 3) if neg > 0 else None,
        "median_mae": pct(g["mae"].median()),
        "p80_mae": pct(g["mae"].quantile(0.8)),
        "median_mfe": pct(g["mfe"].median()),
        "p80_mfe": pct(g["mfe"].quantile(0.8)),
        "big3_rate": pct((g["ret"] >= 0.03).mean()),
        "big5_rate": pct((g["ret"] >= 0.05).mean()),
        "big10_rate": pct((g["ret"] >= 0.10).mean()),
        "loss5_rate": pct((g["ret"] <= -0.05).mean()),
        "median_hold_min": round(float(g["hold_min"].median()), 2),
    }


def render_report(summary: dict[str, Any]) -> str:
    lines = ["# Waterfall Pattern Discovery", ""]
    events = summary.get("events", {})
    if events:
        lines.append(f"Events: {events.get('total', 0)} across {events.get('symbols', 0)} symbols")
        lines += ["", "## Event Families", ""]
        lines += table_from_dict(events.get("by_family", {}), first_col="family")
    trades = summary.get("trades", {})
    if trades:
        lines.append("")
        lines.append(f"Trades: {trades.get('total', 0)} across {trades.get('symbols', 0)} symbols")
        lines += ["", "## Rule Results", ""]
        lines += table_from_dict(trades.get("by_rule", {}), first_col="rule")
        lines += ["", "## Rule / Family Results", ""]
        lines += table_from_dict(trades.get("by_rule_family", {}), first_col="rule_family")
    return "\n".join(lines) + "\n"


def table_from_dict(data: dict[str, Any], first_col: str) -> list[str]:
    if not data:
        return ["No data."]
    keys = list(next(iter(data.values())).keys())
    lines = ["| " + " | ".join([first_col] + keys) + " |", "| " + " | ".join(["---"] * (len(keys) + 1)) + " |"]
    for name, row in sorted(data.items(), key=lambda kv: kv[1].get("profit_factor") or kv[1].get("events") or 0, reverse=True):
        lines.append("| " + " | ".join([str(name)] + [str(row.get(k, "")) for k in keys]) + " |")
    return lines


def pct(value: float) -> float:
    if value is None or not np.isfinite(value):
        return float("nan")
    return round(float(value) * 100.0, 3)


def iso_ms(value: int | float) -> str:
    if value is None or not np.isfinite(value):
        return ""
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(timespec="minutes")


if __name__ == "__main__":
    raise SystemExit(main())
