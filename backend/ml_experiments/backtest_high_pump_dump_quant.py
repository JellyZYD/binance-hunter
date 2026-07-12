"""Backtest a full high-pump volume-dump short strategy.

This experiment is intentionally trade-level rather than signal-level:

1. admit only contracts that are already in a high-pump state;
2. enter short on closed 5m real-body structure breaks with volume expansion;
3. stop on fast reclaim / structure stop;
4. exit dynamically on first-wave exhaustion, rebound, EMA reclaim, or timeout;
5. allow later re-entry if another dump leg starts.

The script does not modify production models or settings.
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
BAR_MS = 5 * 60_000
FEE_ROUND_TRIP = 0.0008


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    entry_tf: str
    pump24: float
    pump12: float
    pump4: float
    pump30m: float
    min_qv30: float
    max_dd_from_high: float
    min_dd_from_high: float
    break_lookback: int
    break_buffer: float
    min_body_drop: float
    min_2bar_drop: float
    close_pos_max: float
    vol_mult: float
    tsell_min: float
    stop_buffer: float
    stop_cap: float
    min_mfe_to_trail: float
    rebound_exit: float
    ema_exit_span: int
    stall_bars: int
    max_hold_bars: int
    cooldown_bars: int
    take_profit: float = 0.0
    profit_lock_start: float = 0.0
    profit_lock: float = 0.0
    dynamic_lock: str = ""
    require_pump24: float = -1.0
    max_pump4_entry: float = 9.0
    max_pump30m_entry: float = 9.0
    trailing_start: float = 0.0
    trailing_callback: float = 0.0
    min_upper_wick: float = 0.0
    max_qv_over_prev6max: float = 99.0


@dataclass
class Trade:
    symbol: str
    config: str
    admit_time: int
    signal_time: int
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    ret: float
    mae: float
    mfe: float
    hold_bars: int
    exit_reason: str
    pump24: float
    pump12: float
    pump4: float
    pump30m: float
    drawdown_from_high: float
    vol_mult: float
    tsell: float
    body_drop: float
    two_bar_drop: float
    signal_kind: str


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.source)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = parquet_files(source, args.max_symbols)
    if not files:
        raise SystemExit(f"no parquet files under {source / 'klines'}")
    data_end = find_data_end(files)
    end = data_end if args.end_ms <= 0 else min(data_end, args.end_ms)
    start = end - args.days * DAY_MS
    configs = build_configs()
    if args.configs:
        wanted = {x.strip() for x in args.configs.split(",") if x.strip()}
        configs = [c for c in configs if c.name in wanted]
        missing = sorted(wanted - {c.name for c in configs})
        if missing:
            raise SystemExit(f"unknown configs: {missing}")

    print(
        json.dumps(
            {
                "source": str(source),
                "files": len(files),
                "days": args.days,
                "start": iso_ms(start),
                "end": iso_ms(end),
                "configs": [c.name for c in configs],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    event_source = resolve_event_source(source, args.event_source)
    event_files = event_file_map(event_source) if args.windowed else {}
    all_trades: list[Trade] = []
    symbol_stats: list[dict[str, Any]] = []
    total_windows = 0
    for idx, path in enumerate(files, 1):
        sym = path.stem.upper()
        windows = [(start, end, start - 2 * DAY_MS, end)]
        if args.windowed:
            event_path = event_files.get(sym)
            if event_path is None:
                continue
            windows = high_pump_windows(
                event_path,
                configs,
                start,
                end,
                args.window_lookback_hours,
                args.window_lookahead_hours,
                args.window_merge_gap_hours,
            )
            if not windows:
                continue
        before = len(all_trades)
        total_windows += len(windows)
        for trade_start, trade_end, load_start, load_end in windows:
            try:
                m1 = load_1m(path, load_start, load_end)
            except Exception as exc:
                print(f"skip {sym} window {iso_ms(trade_start)}: {exc}", flush=True)
                continue
            if m1 is None or len(m1) < 1800:
                continue
            m1 = add_1m_indicators(m1)
            bars = add_indicators(aggregate_5m(m1))
            if len(bars) < 350:
                continue
            for config in configs:
                if config.entry_tf == "1m":
                    all_trades.extend(backtest_symbol_1m(sym, m1, bars, config, trade_start, trade_end))
                else:
                    all_trades.extend(backtest_symbol_5m(sym, bars, config, trade_start, trade_end))
        added = len(all_trades) - before
        if added or args.windowed:
            symbol_stats.append({"symbol": sym, "trades": added, "windows": len(windows)})
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed {idx}/{len(files)} symbols windows={total_windows} trades={len(all_trades)}", flush=True)

    trades_df = trades_to_frame(all_trades)
    summary = summarize(trades_df, configs, start, end, len(files), symbol_stats)
    summary["windowed"] = bool(args.windowed)
    summary["event_source"] = str(event_source) if args.windowed else ""
    summary["event_windows"] = int(total_windows)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trades_path = out_dir / f"high_pump_dump_trades_{stamp}.csv"
    summary_path = out_dir / f"high_pump_dump_summary_{stamp}.json"
    report_path = out_dir / f"high_pump_dump_report_{stamp}.md"
    if not trades_df.empty:
        trades_df.to_csv(trades_path, index=False)
    else:
        trades_path.write_text("", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_report(summary, trades_df), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "summary": str(summary_path), "trades": str(trades_path)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest high-pump volume-dump short strategy.")
    parser.add_argument("--source", default=os.environ.get("HUNTER_BB_SOURCE", r"E:\A\bb\data"))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=0)
    parser.add_argument("--configs", default="")
    parser.add_argument("--out-dir", default="backend/storage/ml/high_pump_dump_quant")
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--windowed", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--event-source", default="", help="15m klines root used to locate high-pump event windows.")
    parser.add_argument("--window-lookback-hours", type=float, default=26.0)
    parser.add_argument("--window-lookahead-hours", type=float, default=72.0)
    parser.add_argument("--window-merge-gap-hours", type=float, default=12.0)
    return parser.parse_args(argv)


def build_configs() -> list[StrategyConfig]:
    common = dict(
        min_qv30=100_000.0,
        min_dd_from_high=0.018,
        break_buffer=0.001,
        close_pos_max=0.45,
        tsell_min=0.52,
        stop_buffer=0.008,
        stop_cap=0.13,
        min_mfe_to_trail=0.035,
        rebound_exit=0.045,
        ema_exit_span=8,
        stall_bars=5,
        max_hold_bars=144,
        cooldown_bars=3,
    )
    return [
        StrategyConfig(
            name="manual_28_fast",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.30,
            break_lookback=4,
            min_body_drop=0.010,
            min_2bar_drop=0.018,
            vol_mult=1.8,
            **common,
        ),
        StrategyConfig(
            name="manual_28_confirmed",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.4,
            **common,
        ),
        StrategyConfig(
            name="manual_28_fast_trail35_stop35",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.30,
            break_lookback=4,
            min_body_drop=0.010,
            min_2bar_drop=0.018,
            vol_mult=1.8,
            min_qv30=100_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=3,
            trailing_start=0.035,
            trailing_callback=0.010,
        ),
        StrategyConfig(
            name="manual_28_confirmed_trail35_stop35",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.4,
            min_qv30=100_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=3,
            trailing_start=0.035,
            trailing_callback=0.010,
        ),
        StrategyConfig(
            name="manual_28_confirmed_trail35_upper",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.4,
            min_qv30=100_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=3,
            trailing_start=0.035,
            trailing_callback=0.010,
            min_upper_wick=0.002,
        ),
        StrategyConfig(
            name="manual_28_confirmed_trail35_upper_dd20",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.20,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.4,
            min_qv30=100_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=3,
            trailing_start=0.035,
            trailing_callback=0.010,
            min_upper_wick=0.002,
        ),
        StrategyConfig(
            name="manual_28_confirmed_trail35_upper_qv",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.4,
            min_qv30=100_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=3,
            trailing_start=0.035,
            trailing_callback=0.010,
            min_upper_wick=0.0029,
            max_qv_over_prev6max=1.75,
        ),
        StrategyConfig(
            name="manual_28_confirmed_trail50_stop35",
            entry_tf="5m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.4,
            min_qv30=100_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=3,
            trailing_start=0.050,
            trailing_callback=0.015,
        ),
        StrategyConfig(
            name="strict_35_confirmed",
            entry_tf="5m",
            pump24=0.35,
            pump12=0.25,
            pump4=0.18,
            pump30m=0.10,
            max_dd_from_high=0.32,
            break_lookback=5,
            min_body_drop=0.014,
            min_2bar_drop=0.024,
            vol_mult=2.2,
            **common,
        ),
        StrategyConfig(
            name="high_40_clean",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.22,
            pump30m=0.12,
            max_dd_from_high=0.35,
            break_lookback=6,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.0,
            min_qv30=150_000.0,
            min_dd_from_high=0.020,
            break_buffer=0.001,
            close_pos_max=0.45,
            tsell_min=0.52,
            stop_buffer=0.010,
            stop_cap=0.14,
            min_mfe_to_trail=0.040,
            rebound_exit=0.050,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=168,
            cooldown_bars=3,
        ),
        StrategyConfig(
            name="high_40_tight_stop",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.26,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.4,
            min_qv30=180_000.0,
            min_dd_from_high=0.020,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.53,
            stop_buffer=0.006,
            stop_cap=0.070,
            min_mfe_to_trail=0.035,
            rebound_exit=0.045,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=144,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="high_40_firstwave",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.26,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.4,
            min_qv30=180_000.0,
            min_dd_from_high=0.020,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.53,
            stop_buffer=0.006,
            stop_cap=0.070,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=96,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="ultra_50_second_leg",
            entry_tf="5m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.42,
            break_lookback=6,
            min_body_drop=0.014,
            min_2bar_drop=0.026,
            vol_mult=1.8,
            min_qv30=150_000.0,
            min_dd_from_high=0.025,
            break_buffer=0.001,
            close_pos_max=0.48,
            tsell_min=0.51,
            stop_buffer=0.012,
            stop_cap=0.16,
            min_mfe_to_trail=0.045,
            rebound_exit=0.060,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=216,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="ultra_50_tight_stop",
            entry_tf="5m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.30,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.030,
            vol_mult=2.2,
            min_qv30=180_000.0,
            min_dd_from_high=0.025,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.52,
            stop_buffer=0.007,
            stop_cap=0.075,
            min_mfe_to_trail=0.040,
            rebound_exit=0.050,
            ema_exit_span=8,
            stall_bars=5,
            max_hold_bars=168,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="fast1m_28_bodybreak",
            entry_tf="1m",
            pump24=0.28,
            pump12=0.20,
            pump4=0.14,
            pump30m=0.08,
            max_dd_from_high=0.30,
            break_lookback=5,
            min_body_drop=0.006,
            min_2bar_drop=0.012,
            vol_mult=3.0,
            min_qv30=100_000.0,
            min_dd_from_high=0.015,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.50,
            stop_buffer=0.009,
            stop_cap=0.13,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=8,
            max_hold_bars=144,
            cooldown_bars=3,
        ),
        StrategyConfig(
            name="fast1m_40_clean",
            entry_tf="1m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.36,
            break_lookback=6,
            min_body_drop=0.007,
            min_2bar_drop=0.014,
            vol_mult=2.6,
            min_qv30=150_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.43,
            tsell_min=0.50,
            stop_buffer=0.010,
            stop_cap=0.14,
            min_mfe_to_trail=0.035,
            rebound_exit=0.045,
            ema_exit_span=8,
            stall_bars=8,
            max_hold_bars=168,
            cooldown_bars=3,
        ),
        StrategyConfig(
            name="fast1m_40_tight_stop",
            entry_tf="1m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.24,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=3.2,
            min_qv30=180_000.0,
            min_dd_from_high=0.018,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.51,
            stop_buffer=0.006,
            stop_cap=0.060,
            min_mfe_to_trail=0.030,
            rebound_exit=0.040,
            ema_exit_span=8,
            stall_bars=8,
            max_hold_bars=144,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="fast1m_50_tight_stop",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=3.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.022,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.51,
            stop_buffer=0.007,
            stop_cap=0.065,
            min_mfe_to_trail=0.035,
            rebound_exit=0.045,
            ema_exit_span=8,
            stall_bars=8,
            max_hold_bars=168,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="fast1m_50_firstwave",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.28,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=3.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.022,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.51,
            stop_buffer=0.007,
            stop_cap=0.065,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=120,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="fast1m_50_quality_top",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.006,
            stop_cap=0.055,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="fast1m_50_quality_tp35",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.006,
            stop_cap=0.055,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
            take_profit=0.035,
        ),
        StrategyConfig(
            name="fast1m_50_quality_tp35_stop35",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
            take_profit=0.035,
        ),
        StrategyConfig(
            name="fast1m_50_quality_dyn_floor",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=120,
            cooldown_bars=4,
            dynamic_lock="floor",
        ),
        StrategyConfig(
            name="fast1m_50_quality_dyn_runner",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.035,
            ema_exit_span=8,
            stall_bars=8,
            max_hold_bars=180,
            cooldown_bars=4,
            dynamic_lock="runner",
        ),
        StrategyConfig(
            name="fast1m_50_quality_dyn_ladder",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
        ),
        StrategyConfig(
            name="fast1m_50_quality_dyn_ladder_stop25",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
        ),
        StrategyConfig(
            name="fast1m_50_quality_dyn_ladder_strong",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.028,
            min_2bar_drop=0.030,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.64,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
            max_pump30m_entry=-0.035,
        ),
        StrategyConfig(
            name="fast1m_50_quality_tp35_stop45",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.005,
            stop_cap=0.045,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
            take_profit=0.035,
        ),
        StrategyConfig(
            name="fast1m_50_quality_tp50",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.006,
            stop_cap=0.055,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
            take_profit=0.050,
        ),
        StrategyConfig(
            name="fast1m_50_quality_lock35",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.080,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=4.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.55,
            stop_buffer=0.006,
            stop_cap=0.055,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
            profit_lock_start=0.050,
            profit_lock=0.035,
        ),
        StrategyConfig(
            name="fast1m_50_quality_sell",
            entry_tf="1m",
            pump24=0.50,
            pump12=0.35,
            pump4=0.25,
            pump30m=0.14,
            max_dd_from_high=0.160,
            break_lookback=5,
            min_body_drop=0.008,
            min_2bar_drop=0.016,
            vol_mult=5.0,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.40,
            tsell_min=0.60,
            stop_buffer=0.006,
            stop_cap=0.055,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=6,
            max_hold_bars=96,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="high_40_quality_top",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.006,
            stop_cap=0.060,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=96,
            cooldown_bars=4,
        ),
        StrategyConfig(
            name="high_40_quality_tp35",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.006,
            stop_cap=0.060,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=96,
            cooldown_bars=4,
            take_profit=0.035,
        ),
        StrategyConfig(
            name="high_40_quality_tp35_stop35",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=96,
            cooldown_bars=4,
            take_profit=0.035,
        ),
        StrategyConfig(
            name="high_40_quality_dyn_floor",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=120,
            cooldown_bars=4,
            dynamic_lock="floor",
        ),
        StrategyConfig(
            name="high_40_quality_dyn_ladder",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
        ),
        StrategyConfig(
            name="high_40_quality_dyn_ladder_stop25",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
        ),
        StrategyConfig(
            name="high_40_quality_dyn_ladder_context",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
            require_pump24=0.32,
            max_pump4_entry=0.15,
        ),
        StrategyConfig(
            name="high_40_quality_dyn_ladder_context_strict",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            dynamic_lock="ladder",
            require_pump24=0.45,
            max_pump4_entry=0.08,
            max_pump30m_entry=-0.02,
        ),
        StrategyConfig(
            name="high_40_quality_trail35_cb10",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            trailing_start=0.035,
            trailing_callback=0.010,
        ),
        StrategyConfig(
            name="high_40_quality_trail50_cb15",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            trailing_start=0.050,
            trailing_callback=0.015,
        ),
        StrategyConfig(
            name="high_40_quality_trail50_cb25",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.004,
            stop_cap=0.035,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            trailing_start=0.050,
            trailing_callback=0.025,
        ),
        StrategyConfig(
            name="high_40_context_trail35_cb10",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            require_pump24=0.32,
            max_pump4_entry=0.15,
            trailing_start=0.035,
            trailing_callback=0.010,
        ),
        StrategyConfig(
            name="high_40_context_trail50_cb15",
            entry_tf="5m",
            pump24=0.40,
            pump12=0.30,
            pump4=0.20,
            pump30m=0.10,
            max_dd_from_high=0.100,
            break_lookback=5,
            min_body_drop=0.016,
            min_2bar_drop=0.028,
            vol_mult=2.5,
            min_qv30=180_000.0,
            min_dd_from_high=0.050,
            break_buffer=0.001,
            close_pos_max=0.42,
            tsell_min=0.55,
            stop_buffer=0.003,
            stop_cap=0.025,
            min_mfe_to_trail=0.030,
            rebound_exit=0.028,
            ema_exit_span=8,
            stall_bars=4,
            max_hold_bars=150,
            cooldown_bars=4,
            require_pump24=0.32,
            max_pump4_entry=0.15,
            trailing_start=0.050,
            trailing_callback=0.015,
        ),
    ]


def parquet_files(source: Path, max_symbols: int) -> list[Path]:
    files = []
    for name in glob.glob(str(source / "klines" / "*.parquet")):
        path = Path(name)
        sym = path.stem.upper()
        if sym.endswith(".PARQUET") or path.name.endswith(".bak"):
            continue
        if sym in EXCLUDE:
            continue
        files.append(path)
    files.sort(key=lambda p: p.stem.upper())
    if max_symbols > 0:
        files = files[:max_symbols]
    return files


def resolve_event_source(source: Path, requested: str) -> Path:
    if requested:
        path = Path(requested)
        if (path / "klines").is_dir():
            return path / "klines"
        return path
    top200 = source / "top200_15m" / "klines"
    if top200.is_dir():
        return top200
    return source / "klines"


def event_file_map(event_source: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for name in glob.glob(str(event_source / "*.parquet")):
        path = Path(name)
        if path.name.endswith(".bak"):
            continue
        sym = path.stem.upper()
        if sym not in EXCLUDE:
            files[sym] = path
    return files


def high_pump_windows(
    event_path: Path,
    configs: list[StrategyConfig],
    start: int,
    end: int,
    lookback_hours: float,
    lookahead_hours: float,
    merge_gap_hours: float,
) -> list[tuple[int, int, int, int]]:
    """Return (trade_start, trade_end, load_start, load_end) windows.

    The 15m file is only used as a lossless speed prefilter. A window qualifies
    when any config could possibly be allowed to trade at that time. The actual
    trade signal still checks 1m/5m current pump conditions.
    """
    min_pump24 = min(c.pump24 for c in configs)
    min_pump12 = min(c.pump12 for c in configs)
    min_pump4 = min(c.pump4 for c in configs)
    min_pump30 = min(c.pump30m for c in configs)
    min_qv30 = min(c.min_qv30 for c in configs)
    try:
        t = pq.read_table(
            event_path,
            columns=["timestamp", "close", "quote_volume"],
            filters=[("timestamp", ">=", start - 2 * DAY_MS), ("timestamp", "<=", end)],
        ).to_pandas()
    except Exception:
        return []
    if t.empty:
        return []
    t = t.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    c = t["close"]
    qv = t["quote_volume"]
    t["pump30m"] = c / c.shift(2) - 1.0
    t["pump4"] = c / c.shift(16) - 1.0
    t["pump12"] = c / c.shift(48) - 1.0
    t["pump24"] = c / c.shift(96) - 1.0
    t["qv30"] = qv.rolling(2).sum()
    mask = (
        (t["timestamp"] >= start)
        & (t["timestamp"] <= end)
        & (t["qv30"] >= min_qv30)
        & (
            (t["pump24"] >= min_pump24)
            | (t["pump12"] >= min_pump12)
            | (t["pump4"] >= min_pump4)
            | (t["pump30m"] >= min_pump30)
        )
    ).fillna(False)
    idx = np.flatnonzero(mask.to_numpy())
    if len(idx) == 0:
        return []
    raw: list[tuple[int, int]] = []
    seg_start = idx[0]
    prev = idx[0]
    for pos in idx[1:]:
        if pos - prev > 1:
            raw.append((int(t.loc[seg_start, "timestamp"]), int(t.loc[prev, "timestamp"] + 15 * 60_000)))
            seg_start = pos
        prev = pos
    raw.append((int(t.loc[seg_start, "timestamp"]), int(t.loc[prev, "timestamp"] + 15 * 60_000)))

    lookback_ms = int(lookback_hours * 60 * 60_000)
    lookahead_ms = int(lookahead_hours * 60 * 60_000)
    merge_gap_ms = int(merge_gap_hours * 60 * 60_000)
    expanded = [
        (max(start, a), min(end, b + lookahead_ms), max(0, a - lookback_ms), min(end, b + lookahead_ms))
        for a, b in raw
    ]
    expanded.sort(key=lambda x: x[2])
    merged: list[tuple[int, int, int, int]] = []
    for trade_start, trade_end, load_start, load_end in expanded:
        if not merged or load_start - merged[-1][3] > merge_gap_ms:
            merged.append((trade_start, trade_end, load_start, load_end))
        else:
            prev_trade_start, prev_trade_end, prev_load_start, prev_load_end = merged[-1]
            merged[-1] = (
                min(prev_trade_start, trade_start),
                max(prev_trade_end, trade_end),
                min(prev_load_start, load_start),
                max(prev_load_end, load_end),
            )
    return merged


def find_data_end(files: list[Path]) -> int:
    end = 0
    for path in files:
        try:
            pf = pq.ParquetFile(path)
            for i in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(i).column(0).statistics
                if st and st.has_min_max:
                    end = max(end, int(st.max))
        except Exception:
            continue
    if not end:
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
    return end


def load_1m(path: Path, start: int, end: int) -> pd.DataFrame | None:
    table = pq.read_table(
        path,
        columns=["timestamp", "open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume"],
        filters=[("timestamp", ">=", start), ("timestamp", "<=", end)],
    ).to_pandas()
    if table.empty:
        return None
    table = table.drop_duplicates("timestamp").sort_values("timestamp")
    table = table.rename(
        columns={
            "timestamp": "b",
            "quote_volume": "qv",
            "taker_buy_quote_volume": "tbq",
        }
    )
    return table[["b", "open", "high", "low", "close", "qv", "tbq"]]


def aggregate_5m(m1: pd.DataFrame) -> pd.DataFrame:
    table = m1.copy()
    table["b5"] = (table["b"] // BAR_MS) * BAR_MS
    grouped = table.groupby("b5", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        qv=("qv", "sum"),
        tbq=("tbq", "sum"),
        cnt=("close", "size"),
    )
    grouped = grouped[grouped["cnt"] >= 4].reset_index().rename(columns={"b5": "b"})
    return grouped


def add_1m_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    h = out["high"]
    low = out["low"]
    o = out["open"]
    qv = out["qv"]
    body_low = np.minimum(o, c)
    body_high = np.maximum(o, c)
    rng = (h - low).replace(0, np.nan)
    out["body_low"] = body_low
    out["body_high"] = body_high
    out["close_pos"] = (c - low) / rng
    out["body_drop"] = (o - c) / o
    out["pump30m"] = c / c.shift(30) - 1.0
    out["pump4"] = c / c.shift(240) - 1.0
    out["pump12"] = c / c.shift(720) - 1.0
    out["pump24"] = c / c.shift(1440) - 1.0
    out["qv30"] = qv.rolling(30).sum()
    out["volr20"] = qv / qv.rolling(20).mean()
    out["volr3_60"] = qv.rolling(3).sum() / qv.rolling(60).mean().rolling(3).sum()
    out["tsell"] = 1.0 - (out["tbq"] / qv.replace(0, np.nan))
    out["ema8"] = c.ewm(span=8, adjust=False).mean()
    out["ema13"] = c.ewm(span=13, adjust=False).mean()
    out["two_bar_drop"] = c / c.shift(2) - 1.0
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    h = out["high"]
    low = out["low"]
    o = out["open"]
    qv = out["qv"]
    body_low = np.minimum(o, c)
    body_high = np.maximum(o, c)
    rng = (h - low).replace(0, np.nan)
    out["body_low"] = body_low
    out["body_high"] = body_high
    out["close_pos"] = (c - low) / rng
    out["body_drop"] = (o - c) / o
    out["ret_2"] = c / c.shift(2) - 1.0
    out["pump30m"] = c / c.shift(6) - 1.0
    out["pump4"] = c / c.shift(48) - 1.0
    out["pump12"] = c / c.shift(144) - 1.0
    out["pump24"] = c / c.shift(288) - 1.0
    out["runup24"] = c / low.rolling(288).min() - 1.0
    out["dd_high24"] = c / h.rolling(288).max() - 1.0
    out["qv30"] = qv.rolling(6).sum()
    out["volr20"] = qv / qv.rolling(20).mean()
    out["volr48"] = qv / qv.rolling(48).mean()
    out["volr2_20"] = qv.rolling(2).sum() / qv.rolling(20).mean().rolling(2).sum()
    out["tsell"] = 1.0 - (out["tbq"] / qv.replace(0, np.nan))
    out["ema8"] = c.ewm(span=8, adjust=False).mean()
    out["ema13"] = c.ewm(span=13, adjust=False).mean()
    out["atr14"] = ((h - low) / c).rolling(14).mean()
    out["two_bar_drop"] = c / c.shift(2) - 1.0
    for n in (4, 5, 6):
        out[f"prior_body_low_{n}"] = body_low.rolling(n).min().shift(1)
        out[f"prior_body_high_{n}"] = body_high.rolling(n).max().shift(1)
    return out


def backtest_symbol_5m(symbol: str, bars: pd.DataFrame, config: StrategyConfig, start: int, end: int) -> list[Trade]:
    trades: list[Trade] = []
    n = len(bars)
    active = False
    admit_time = 0
    high_since_admit = -math.inf
    cooldown_until = -1
    i = 288
    while i < n - 2:
        row = bars.iloc[i]
        if int(row["b"]) < start:
            i += 1
            continue
        if int(row["b"]) > end:
            break
        high_pump = (
            row["pump24"] >= config.pump24
            or row["pump12"] >= config.pump12
            or row["pump4"] >= config.pump4
            or row["pump30m"] >= config.pump30m
        )
        liquid = row["qv30"] >= config.min_qv30
        if high_pump and liquid and not active:
            active = True
            admit_time = int(row["b"])
            high_since_admit = float(row["high"])
        if active:
            high_since_admit = max(high_since_admit, float(row["high"]))
            dd = 1.0 - float(row["close"]) / high_since_admit if high_since_admit > 0 else np.nan
            stale = int(row["b"]) - admit_time > 72 * 60 * 60_000 and dd > 0.45
            if stale:
                active = False
                i += 1
                continue
            if i >= cooldown_until and is_entry_signal(bars, i, config, dd):
                trade = simulate_trade(symbol, bars, i, config, admit_time, high_since_admit)
                if trade is not None:
                    trades.append(trade)
                    exit_pos = int(bars.index[bars["b"] == trade.exit_time][0]) if (bars["b"] == trade.exit_time).any() else i
                    cooldown_until = exit_pos + config.cooldown_bars
                    i = max(i + 1, exit_pos + 1)
                    continue
        i += 1
    return trades


def backtest_symbol_1m(symbol: str, m1: pd.DataFrame, m5: pd.DataFrame, config: StrategyConfig, start: int, end: int) -> list[Trade]:
    trades: list[Trade] = []
    if m1.empty or m5.empty:
        return trades
    m5_by_time = {int(t): pos for pos, t in enumerate(m5["b"].astype("int64").to_numpy())}
    n = len(m1)
    active = False
    admit_time = 0
    high_since_admit = -math.inf
    cooldown_until = -1
    i = 1440
    while i < n - 2:
        row = m1.iloc[i]
        t = int(row["b"])
        if t < start:
            i += 1
            continue
        if t > end:
            break
        high_pump = (
            row["pump24"] >= config.pump24
            or row["pump12"] >= config.pump12
            or row["pump4"] >= config.pump4
            or row["pump30m"] >= config.pump30m
        )
        liquid = row["qv30"] >= config.min_qv30
        if high_pump and liquid and not active:
            active = True
            admit_time = t
            high_since_admit = float(row["high"])
        if active:
            high_since_admit = max(high_since_admit, float(row["high"]))
            dd = 1.0 - float(row["close"]) / high_since_admit if high_since_admit > 0 else np.nan
            stale = t - admit_time > 72 * 60 * 60_000 and dd > 0.45
            if stale:
                active = False
                i += 1
                continue
            ctx_pos = latest_completed_5m_pos(t, m5_by_time)
            if ctx_pos is not None and i >= cooldown_until and is_entry_signal_1m(m1, m5, i, ctx_pos, config, dd):
                trade = simulate_trade_1m(symbol, m1, m5, i, ctx_pos, config, admit_time, high_since_admit)
                if trade is not None:
                    trades.append(trade)
                    exit_pos = int(m1.index[m1["b"] == trade.exit_time][0]) if (m1["b"] == trade.exit_time).any() else i
                    cooldown_until = exit_pos + config.cooldown_bars * 5
                    i = max(i + 1, exit_pos + 1)
                    continue
        i += 1
    return trades


def latest_completed_5m_pos(one_min_open: int, m5_by_time: dict[int, int]) -> int | None:
    one_min_close = one_min_open + 60_000
    latest_open = ((one_min_close - BAR_MS) // BAR_MS) * BAR_MS
    return m5_by_time.get(int(latest_open))


def is_entry_signal(bars: pd.DataFrame, i: int, config: StrategyConfig, dd_from_high: float) -> bool:
    row = bars.iloc[i]
    if not current_pump_ok(row, config):
        return False
    if row["pump24"] < config.require_pump24:
        return False
    if row["pump4"] > config.max_pump4_entry:
        return False
    if row["pump30m"] > config.max_pump30m_entry:
        return False
    if not np.isfinite(dd_from_high):
        return False
    if dd_from_high < config.min_dd_from_high or dd_from_high > config.max_dd_from_high:
        return False
    if row["qv30"] < config.min_qv30:
        return False
    if row["close_pos"] > config.close_pos_max:
        return False
    if row["tsell"] < config.tsell_min:
        return False
    upper_wick = (float(row["high"]) - max(float(row["open"]), float(row["close"]))) / float(row["open"])
    if upper_wick < config.min_upper_wick:
        return False
    prev6_qv = float(bars.iloc[max(0, i - 6): i]["qv"].max()) if i > 0 else float("nan")
    if np.isfinite(prev6_qv) and prev6_qv > 0:
        if float(row["qv"]) / prev6_qv > config.max_qv_over_prev6max:
            return False
    body_break = row["close"] < row[f"prior_body_low_{config.break_lookback}"] * (1.0 - config.break_buffer)
    one_bar = (
        row["body_drop"] >= config.min_body_drop
        and row["volr20"] >= config.vol_mult
        and body_break
    )
    two_bar = (
        -row["two_bar_drop"] >= config.min_2bar_drop
        and row["volr2_20"] >= config.vol_mult
        and body_break
        and row["close"] < bars.iloc[i - 1]["close"]
    )
    return bool(one_bar or two_bar)


def is_entry_signal_1m(m1: pd.DataFrame, m5: pd.DataFrame, i: int, ctx_pos: int, config: StrategyConfig, dd_from_high: float) -> bool:
    row = m1.iloc[i]
    ctx = m5.iloc[ctx_pos]
    if not current_pump_ok(row, config):
        return False
    if row["pump24"] < config.require_pump24:
        return False
    if row["pump4"] > config.max_pump4_entry:
        return False
    if row["pump30m"] > config.max_pump30m_entry:
        return False
    if not np.isfinite(dd_from_high):
        return False
    if dd_from_high < config.min_dd_from_high or dd_from_high > config.max_dd_from_high:
        return False
    if row["qv30"] < config.min_qv30:
        return False
    if row["close_pos"] > config.close_pos_max:
        return False
    if row["tsell"] < config.tsell_min:
        return False
    upper_wick = (float(row["high"]) - max(float(row["open"]), float(row["close"]))) / float(row["open"])
    if upper_wick < config.min_upper_wick:
        return False
    prev6_qv = float(m1.iloc[max(0, i - 6): i]["qv"].max()) if i > 0 else float("nan")
    if np.isfinite(prev6_qv) and prev6_qv > 0:
        if float(row["qv"]) / prev6_qv > config.max_qv_over_prev6max:
            return False
    if not np.isfinite(ctx[f"prior_body_low_{config.break_lookback}"]):
        return False
    body_break = row["close"] < ctx[f"prior_body_low_{config.break_lookback}"] * (1.0 - config.break_buffer)
    one_bar = (
        row["body_drop"] >= config.min_body_drop
        and row["volr20"] >= config.vol_mult
        and body_break
    )
    two_bar = (
        -row["two_bar_drop"] >= config.min_2bar_drop
        and row["volr3_60"] >= config.vol_mult
        and body_break
        and row["close"] < m1.iloc[i - 1]["close"]
    )
    return bool(one_bar or two_bar)


def current_pump_ok(row: pd.Series, config: StrategyConfig) -> bool:
    return bool(
        row["pump24"] >= config.pump24
        or row["pump12"] >= config.pump12
        or row["pump4"] >= config.pump4
        or row["pump30m"] >= config.pump30m
    )


def dynamic_lock_return(mfe: float, style: str) -> float:
    if style == "floor":
        if mfe >= 0.18:
            return 0.13
        if mfe >= 0.13:
            return 0.09
        if mfe >= 0.09:
            return 0.065
        if mfe >= 0.065:
            return 0.047
        if mfe >= 0.045:
            return 0.032
        return 0.0
    if style == "runner":
        if mfe >= 0.22:
            return 0.16
        if mfe >= 0.16:
            return 0.11
        if mfe >= 0.12:
            return 0.075
        if mfe >= 0.085:
            return 0.052
        if mfe >= 0.060:
            return 0.035
        if mfe >= 0.045:
            return 0.022
        return 0.0
    if style == "ladder":
        if mfe >= 0.22:
            return 0.16
        if mfe >= 0.18:
            return 0.13
        if mfe >= 0.14:
            return 0.10
        if mfe >= 0.105:
            return 0.075
        if mfe >= 0.075:
            return 0.055
        if mfe >= 0.050:
            return 0.040
        if mfe >= 0.035:
            return 0.030
        return 0.0
    return 0.0


def simulate_trade(symbol: str, bars: pd.DataFrame, signal_i: int, config: StrategyConfig, admit_time: int, high_since_admit: float) -> Trade | None:
    if signal_i + 1 >= len(bars):
        return None
    sig = bars.iloc[signal_i]
    entry_i = signal_i + 1
    entry_row = bars.iloc[entry_i]
    entry = float(entry_row["open"])
    if not np.isfinite(entry) or entry <= 0:
        return None
    recent_high = float(bars.iloc[max(0, signal_i - config.break_lookback + 1): signal_i + 1]["body_high"].max())
    stop = max(float(sig["high"]), recent_high) * (1.0 + config.stop_buffer)
    stop = min(stop, entry * (1.0 + config.stop_cap))
    breakdown_level = float(sig[f"prior_body_low_{config.break_lookback}"])
    best_low = entry
    worst_high = entry
    profit_lock_active = False
    trailing_active = False
    trailing_low = entry
    exit_price = float(bars.iloc[min(entry_i + config.max_hold_bars, len(bars) - 1)]["close"])
    exit_i = min(entry_i + config.max_hold_bars, len(bars) - 1)
    reason = "timeout"
    no_new_low = 0
    for j in range(entry_i, min(len(bars), entry_i + config.max_hold_bars + 1)):
        row = bars.iloc[j]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        worst_high = max(worst_high, high)
        if low < best_low:
            best_low = low
            no_new_low = 0
        else:
            no_new_low += 1

        if high >= stop:
            exit_price = stop
            exit_i = j
            reason = "stop"
            break

        mfe = entry / best_low - 1.0
        if config.take_profit > 0:
            take_price = entry / (1.0 + config.take_profit)
            if low <= take_price:
                exit_price = take_price
                exit_i = j
                reason = "take_profit"
                break
        if config.profit_lock_start > 0 and mfe >= config.profit_lock_start:
            profit_lock_active = True
        if profit_lock_active and config.profit_lock > 0:
            lock_price = entry / (1.0 + config.profit_lock)
            if high >= lock_price:
                exit_price = lock_price
                exit_i = j
                reason = "profit_lock"
                break
        dyn_lock = dynamic_lock_return(mfe, config.dynamic_lock)
        if dyn_lock > 0:
            lock_price = entry / (1.0 + dyn_lock)
            if high >= lock_price:
                exit_price = lock_price
                exit_i = j
                reason = "dynamic_lock"
                break
        just_activated_trailing = False
        if config.trailing_start > 0 and config.trailing_callback > 0:
            if not trailing_active and mfe >= config.trailing_start:
                trailing_active = True
                trailing_low = best_low
                just_activated_trailing = True
            elif trailing_active:
                trailing_low = min(trailing_low, low)
            if trailing_active and not just_activated_trailing:
                trailing_price = trailing_low * (1.0 + config.trailing_callback)
                if high >= trailing_price:
                    exit_price = trailing_price
                    exit_i = j
                    reason = "trailing_stop"
                    break
        if j <= entry_i + 2 and close > breakdown_level * 1.006:
            exit_price = close
            exit_i = j
            reason = "quick_reclaim"
            break

        if mfe >= config.min_mfe_to_trail:
            rebound = close / best_low - 1.0
            ema_col = "ema8" if config.ema_exit_span == 8 else "ema13"
            ema_reclaim = close > float(row[ema_col]) and row["close_pos"] >= 0.62
            stall_exit = no_new_low >= config.stall_bars and rebound >= config.rebound_exit * 0.55
            if rebound >= config.rebound_exit:
                exit_price = close
                exit_i = j
                reason = "rebound_trail"
                break
            if ema_reclaim:
                exit_price = close
                exit_i = j
                reason = "ema_reclaim"
                break
            if stall_exit:
                exit_price = close
                exit_i = j
                reason = "stall"
                break

    exit_time = int(bars.iloc[exit_i]["b"])
    ret = 1.0 - exit_price / entry - FEE_ROUND_TRIP
    mae = worst_high / entry - 1.0
    mfe = entry / best_low - 1.0
    dd = 1.0 - float(sig["close"]) / high_since_admit if high_since_admit > 0 else float("nan")
    one_bar = sig["body_drop"] >= config.min_body_drop and sig["volr20"] >= config.vol_mult
    signal_kind = "one_bar" if one_bar else "two_bar"
    return Trade(
        symbol=symbol,
        config=config.name,
        admit_time=admit_time,
        signal_time=int(sig["b"]),
        entry_time=int(entry_row["b"]),
        exit_time=exit_time,
        entry=entry,
        exit=exit_price,
        ret=ret,
        mae=mae,
        mfe=mfe,
        hold_bars=max(1, exit_i - entry_i + 1),
        exit_reason=reason,
        pump24=float(sig["pump24"]),
        pump12=float(sig["pump12"]),
        pump4=float(sig["pump4"]),
        pump30m=float(sig["pump30m"]),
        drawdown_from_high=dd,
        vol_mult=float(max(sig["volr20"], sig["volr2_20"])),
        tsell=float(sig["tsell"]),
        body_drop=float(sig["body_drop"]),
        two_bar_drop=float(-sig["two_bar_drop"]),
        signal_kind=signal_kind,
    )


def simulate_trade_1m(
    symbol: str,
    m1: pd.DataFrame,
    m5: pd.DataFrame,
    signal_i: int,
    ctx_pos: int,
    config: StrategyConfig,
    admit_time: int,
    high_since_admit: float,
) -> Trade | None:
    if signal_i + 1 >= len(m1):
        return None
    sig = m1.iloc[signal_i]
    ctx = m5.iloc[ctx_pos]
    entry_i = signal_i + 1
    entry_row = m1.iloc[entry_i]
    entry = float(entry_row["open"])
    if not np.isfinite(entry) or entry <= 0:
        return None
    recent_1m_high = float(m1.iloc[max(0, signal_i - config.break_lookback * 5 + 1): signal_i + 1]["body_high"].max())
    recent_5m_high = float(ctx[f"prior_body_high_{config.break_lookback}"])
    stop = max(float(sig["high"]), recent_1m_high, recent_5m_high) * (1.0 + config.stop_buffer)
    stop = min(stop, entry * (1.0 + config.stop_cap))
    breakdown_level = float(ctx[f"prior_body_low_{config.break_lookback}"])
    best_low = entry
    worst_high = entry
    profit_lock_active = False
    trailing_active = False
    trailing_low = entry
    max_hold = config.max_hold_bars * 5
    stall_bars = max(config.stall_bars, 8)
    exit_price = float(m1.iloc[min(entry_i + max_hold, len(m1) - 1)]["close"])
    exit_i = min(entry_i + max_hold, len(m1) - 1)
    reason = "timeout"
    no_new_low = 0
    for j in range(entry_i, min(len(m1), entry_i + max_hold + 1)):
        row = m1.iloc[j]
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        worst_high = max(worst_high, high)
        if low < best_low:
            best_low = low
            no_new_low = 0
        else:
            no_new_low += 1

        if high >= stop:
            exit_price = stop
            exit_i = j
            reason = "stop"
            break

        mfe = entry / best_low - 1.0
        if config.take_profit > 0:
            take_price = entry / (1.0 + config.take_profit)
            if low <= take_price:
                exit_price = take_price
                exit_i = j
                reason = "take_profit"
                break
        if config.profit_lock_start > 0 and mfe >= config.profit_lock_start:
            profit_lock_active = True
        if profit_lock_active and config.profit_lock > 0:
            lock_price = entry / (1.0 + config.profit_lock)
            if high >= lock_price:
                exit_price = lock_price
                exit_i = j
                reason = "profit_lock"
                break
        dyn_lock = dynamic_lock_return(mfe, config.dynamic_lock)
        if dyn_lock > 0:
            lock_price = entry / (1.0 + dyn_lock)
            if high >= lock_price:
                exit_price = lock_price
                exit_i = j
                reason = "dynamic_lock"
                break
        just_activated_trailing = False
        if config.trailing_start > 0 and config.trailing_callback > 0:
            if not trailing_active and mfe >= config.trailing_start:
                trailing_active = True
                trailing_low = best_low
                just_activated_trailing = True
            elif trailing_active:
                trailing_low = min(trailing_low, low)
            if trailing_active and not just_activated_trailing:
                trailing_price = trailing_low * (1.0 + config.trailing_callback)
                if high >= trailing_price:
                    exit_price = trailing_price
                    exit_i = j
                    reason = "trailing_stop"
                    break
        if j <= entry_i + 3 and close > breakdown_level * 1.006:
            exit_price = close
            exit_i = j
            reason = "quick_reclaim"
            break

        if mfe >= config.min_mfe_to_trail:
            rebound = close / best_low - 1.0
            ema_col = "ema8" if config.ema_exit_span == 8 else "ema13"
            ema_reclaim = close > float(row[ema_col]) and row["close_pos"] >= 0.65
            stall_exit = no_new_low >= stall_bars and rebound >= config.rebound_exit * 0.45
            if rebound >= config.rebound_exit:
                exit_price = close
                exit_i = j
                reason = "rebound_trail"
                break
            if ema_reclaim and no_new_low >= 3:
                exit_price = close
                exit_i = j
                reason = "ema_reclaim"
                break
            if stall_exit:
                exit_price = close
                exit_i = j
                reason = "stall"
                break

    exit_time = int(m1.iloc[exit_i]["b"])
    ret = 1.0 - exit_price / entry - FEE_ROUND_TRIP
    mae = worst_high / entry - 1.0
    mfe = entry / best_low - 1.0
    dd = 1.0 - float(sig["close"]) / high_since_admit if high_since_admit > 0 else float("nan")
    one_bar = sig["body_drop"] >= config.min_body_drop and sig["volr20"] >= config.vol_mult
    signal_kind = "1m_one_bar" if one_bar else "1m_two_bar"
    return Trade(
        symbol=symbol,
        config=config.name,
        admit_time=admit_time,
        signal_time=int(sig["b"]),
        entry_time=int(entry_row["b"]),
        exit_time=exit_time,
        entry=entry,
        exit=exit_price,
        ret=ret,
        mae=mae,
        mfe=mfe,
        hold_bars=max(1, exit_i - entry_i + 1),
        exit_reason=reason,
        pump24=float(sig["pump24"]),
        pump12=float(sig["pump12"]),
        pump4=float(sig["pump4"]),
        pump30m=float(sig["pump30m"]),
        drawdown_from_high=dd,
        vol_mult=float(max(sig["volr20"], sig["volr3_60"])),
        tsell=float(sig["tsell"]),
        body_drop=float(sig["body_drop"]),
        two_bar_drop=float(-sig["two_bar_drop"]),
        signal_kind=signal_kind,
    )


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    rows = [asdict(t) for t in trades]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in ("admit_time", "signal_time", "entry_time", "exit_time"):
        df[col + "_iso"] = df[col].map(iso_ms)
    return df


def summarize(trades: pd.DataFrame, configs: list[StrategyConfig], start: int, end: int, symbols: int, symbol_stats: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "period": {"start": iso_ms(start), "end": iso_ms(end), "days": round((end - start) / DAY_MS, 2)},
        "symbols": symbols,
        "configs": [asdict(c) for c in configs],
        "summary": {},
        "symbol_stats_top": sorted(symbol_stats, key=lambda x: x["trades"], reverse=True)[:30],
    }
    if trades.empty:
        return result
    for config, group in trades.groupby("config"):
        result["summary"][config] = summarize_group(group)
    result["by_exit_reason"] = {
        cfg: {k: summarize_group(v) for k, v in g.groupby("exit_reason")}
        for cfg, g in trades.groupby("config")
    }
    return result


def summarize_group(g: pd.DataFrame) -> dict[str, Any]:
    wins = g[g["ret"] > 0]
    losses = g[g["ret"] <= 0]
    gross_win = float(wins["ret"].sum()) if len(wins) else 0.0
    gross_loss = float(-losses["ret"].sum()) if len(losses) else 0.0
    return {
        "trades": int(len(g)),
        "symbols": int(g["symbol"].nunique()),
        "win_rate": pct((g["ret"] > 0).mean()),
        "avg_ret": pct(g["ret"].mean()),
        "median_ret": pct(g["ret"].median()),
        "total_ret_sum": pct(g["ret"].sum()),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "median_mae": pct(g["mae"].median()),
        "p80_mae": pct(g["mae"].quantile(0.80)),
        "median_mfe": pct(g["mfe"].median()),
        "p80_mfe": pct(g["mfe"].quantile(0.80)),
        "big5_rate": pct((g["ret"] >= 0.05).mean()),
        "big10_rate": pct((g["ret"] >= 0.10).mean()),
        "big15_rate": pct((g["ret"] >= 0.15).mean()),
        "loss5_rate": pct((g["ret"] <= -0.05).mean()),
        "median_hold_bars": float(g["hold_bars"].median()),
        "median_hold_hours": round(float(g["hold_bars"].median()) * 5 / 60, 2),
    }


def render_report(summary: dict[str, Any], trades: pd.DataFrame) -> str:
    lines = [
        "# High Pump Volume-Dump Quant Backtest",
        "",
        f"Period: {summary['period']['start']} to {summary['period']['end']} ({summary['period']['days']} days)",
        f"Symbols scanned: {summary['symbols']}",
        "",
        "## Strategy",
        "",
        "- Admission: rolling 24h/12h/4h/30m pump threshold plus 30m quote-volume floor.",
        "- Entry: closed 5m bearish real-body structure break, high volume expansion, low close position, taker-sell support.",
        "- Stop: structure high plus buffer, capped by max stop percentage; fast reclaim exits early.",
        "- Exit: after sufficient MFE, trail by rebound from best low, EMA reclaim, stall, or timeout.",
        "- Re-entry: allowed after cooldown so second/third dump legs can be captured.",
        "",
        "## Config Results",
        "",
    ]
    if not summary["summary"]:
        lines.append("No trades.")
        return "\n".join(lines)
    header = "| config | trades | win | avg | med | PF | med MAE | p80 MAE | med MFE | big>=5 | big>=10 | hold h |"
    lines += [header, "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in sorted(summary["summary"].items(), key=lambda kv: kv[1]["profit_factor"] or 0, reverse=True):
        lines.append(
            f"| {name} | {row['trades']} | {row['win_rate']} | {row['avg_ret']} | {row['median_ret']} | "
            f"{row['profit_factor']} | {row['median_mae']} | {row['p80_mae']} | {row['median_mfe']} | "
            f"{row['big5_rate']} | {row['big10_rate']} | {row['median_hold_hours']} |"
        )
    lines += ["", "## Exit Reasons", ""]
    for cfg, reasons in summary.get("by_exit_reason", {}).items():
        lines.append(f"### {cfg}")
        lines.append("| reason | trades | win | avg | med | med MAE | med MFE |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for reason, row in sorted(reasons.items(), key=lambda kv: kv[1]["trades"], reverse=True):
            lines.append(
                f"| {reason} | {row['trades']} | {row['win_rate']} | {row['avg_ret']} | {row['median_ret']} | "
                f"{row['median_mae']} | {row['median_mfe']} |"
            )
        lines.append("")
    if not trades.empty:
        best = trades.sort_values("ret", ascending=False).head(20)
        worst = trades.sort_values("ret", ascending=True).head(20)
        lines += ["## Best Trades", ""]
        lines += frame_to_md(best[["symbol", "config", "entry_time_iso", "exit_time_iso", "ret", "mae", "mfe", "exit_reason"]])
        lines += ["", "## Worst Trades", ""]
        lines += frame_to_md(worst[["symbol", "config", "entry_time_iso", "exit_time_iso", "ret", "mae", "mfe", "exit_reason"]])
    return "\n".join(lines) + "\n"


def frame_to_md(df: pd.DataFrame) -> list[str]:
    out = df.copy()
    for col in ("ret", "mae", "mfe"):
        out[col] = out[col].map(lambda x: pct(float(x)))
    header = "| " + " | ".join(out.columns) + " |"
    sep = "| " + " | ".join(["---"] * len(out.columns)) + " |"
    lines = [header, sep]
    for _, row in out.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in out.columns) + " |")
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
