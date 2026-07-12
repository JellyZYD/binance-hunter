"""Resimulate 1m waterfall candidate entries with exit/stop profiles.

The candidate entries come from discover_waterfall_patterns.py. This script
keeps the same entry signals, changes only stop/trailing/timeout behavior, then
replays combo-level conflicts/cooldowns. It is an experiment, not production
code.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backtest_waterfall_combo import CONFIGS, ComboConfig, metrics, period_metrics, replay_combo  # noqa: E402
from discover_waterfall_patterns import FEE_ROUND_TRIP, build_rules, load_symbol, parquet_files  # noqa: E402


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_cap: float
    stop_body_high_buffer: float
    trail_activate: float
    trail_rebound: float
    quick_reclaim_buffer: float
    rebound_activate: float
    rebound_retrace: float
    max_hold_min: int


PROFILE_GRID = [
    ExitProfile("tight_18_fast", 0.018, 0.0020, 0.020, 0.006, 0.0015, 0.018, 0.010, 90),
    ExitProfile("tight_20_fast", 0.020, 0.0020, 0.020, 0.007, 0.0020, 0.020, 0.012, 90),
    ExitProfile("tight_22_balanced", 0.022, 0.0025, 0.025, 0.008, 0.0020, 0.022, 0.014, 120),
    ExitProfile("tight_25_balanced", 0.025, 0.0025, 0.025, 0.008, 0.0025, 0.025, 0.016, 150),
    ExitProfile("medium_28_lock", 0.028, 0.0030, 0.028, 0.009, 0.0025, 0.025, 0.016, 180),
    ExitProfile("medium_30_lock", 0.030, 0.0030, 0.030, 0.010, 0.0030, 0.028, 0.018, 180),
    ExitProfile("loose_35_trend", 0.035, 0.0040, 0.035, 0.012, 0.0040, 0.032, 0.022, 240),
    ExitProfile("let_big_run", 0.035, 0.0040, 0.050, 0.015, 0.0040, 0.040, 0.026, 360),
    ExitProfile("quick_cut_trail", 0.022, 0.0020, 0.018, 0.006, 0.0015, 0.018, 0.010, 120),
    ExitProfile("anti_spike_25", 0.025, 0.0035, 0.030, 0.008, 0.0025, 0.025, 0.014, 180),
    ExitProfile("small_wave_lock", 0.020, 0.0025, 0.015, 0.005, 0.0015, 0.015, 0.008, 90),
    ExitProfile("dynamic_step_like", 0.030, 0.0030, 0.035, 0.010, 0.0025, 0.025, 0.014, 240),
]


FOCUS_CONFIGS = (
    "combo_recent_stable",
    "combo_recent_core_2h",
    "combo_recent_core_4h",
    "combo_recent_quality_2h",
    "combo_recent_high_pf_2h",
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(args.trades)
    if args.rule_include:
        keep = {x.strip() for x in args.rule_include.split(",") if x.strip()}
        candidates = candidates[candidates["rule"].isin(keep)].copy()
    candidates = candidates.sort_values(["symbol", "entry_time", "rule"]).reset_index(drop=True)
    if candidates.empty:
        raise SystemExit("empty candidates")

    rule_map = {rule.name: rule for rule in build_rules()}
    configs = [cfg for cfg in CONFIGS if not args.configs or cfg.name in set(args.configs.split(","))]
    replayed_by_profile = resimulate_all_profiles(candidates, Path(args.source), args.days, rule_map, PROFILE_GRID, args.progress_every)
    original_candidates = candidates.copy()
    original_candidates["exit_profile"] = "original"

    profile_rows: list[dict[str, Any]] = []
    selected_by_name: dict[str, pd.DataFrame] = {}
    for profile in PROFILE_GRID:
        replayed = pd.DataFrame(replayed_by_profile.get(profile.name, []))
        if replayed.empty:
            continue
        for config in configs:
            selected = replay_combo(replayed, config)
            key = f"{profile.name}__{config.name}"
            selected_by_name[key] = selected
            row = {"profile": profile.name, "config": config.name, **metrics(selected)}
            profile_rows.append(row)
            for p_row in period_metrics(selected, config.name):
                p_row["profile"] = profile.name
                profile_rows.append(p_row)

    summary = pd.DataFrame(profile_rows)
    if not summary.empty:
        summary = summary.sort_values(["period", "profit_factor", "avg_ret"], ascending=[True, False, False])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"waterfall_exit_profile_summary_{stamp}.csv"
    report_path = out_dir / f"waterfall_exit_profile_report_{stamp}.md"
    replayed_path = out_dir / f"waterfall_exit_profile_candidates_{stamp}.csv"
    summary.to_csv(csv_path, index=False)
    report_path.write_text(render_report(summary), encoding="utf-8")
    replayed_parts = [original_candidates]
    replayed_parts.extend(pd.DataFrame(rows) for rows in replayed_by_profile.values() if rows)
    pd.concat(replayed_parts, ignore_index=True).to_csv(replayed_path, index=False)
    for key, df in selected_by_name.items():
        if not df.empty and key.endswith("__combo_recent_quality_2h"):
            df.to_csv(out_dir / f"waterfall_exit_profile_{key}_{stamp}.csv", index=False)
    print(json.dumps({"summary": str(csv_path), "report": str(report_path), "candidates": str(replayed_path)}, ensure_ascii=False), flush=True)
    return 0


def resimulate_all_profiles(
    candidates: pd.DataFrame,
    source: Path,
    days: int,
    rule_map: dict[str, Any],
    profiles: list[ExitProfile],
    progress_every: int,
) -> dict[str, list[dict[str, Any]]]:
    symbols = set(candidates["symbol"].astype(str))
    files = {path.stem.upper(): path for path in parquet_files(source, 0) if path.stem.upper() in symbols}
    out: dict[str, list[dict[str, Any]]] = {profile.name: [] for profile in profiles}
    groups = list(candidates.groupby("symbol", sort=False))
    for idx, (symbol, group) in enumerate(groups, 1):
        path = files.get(str(symbol))
        if path is None:
            continue
        try:
            df = load_symbol(path, days).reset_index(drop=True)
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)
            continue
        if df.empty:
            continue
        pos_by_t = {int(t): i for i, t in enumerate(df["t"].to_numpy())}
        for _, cand in group.iterrows():
            rule = rule_map.get(str(cand["rule"]))
            if rule is None:
                continue
            signal_i = pos_by_t.get(int(cand["signal_time"]))
            if signal_i is None:
                continue
            base = cand.to_dict()
            for profile in profiles:
                sim = simulate_exit(df, signal_i, rule, profile)
                if sim is None:
                    continue
                row = dict(base)
                row.update(sim)
                row["exit_profile"] = profile.name
                out[profile.name].append(row)
        del df
        if progress_every and idx % progress_every == 0:
            done = sum(len(rows) for rows in out.values())
            print(f"processed {idx}/{len(groups)} profile_rows={done}", flush=True)
    return out


def resimulate_candidates(
    candidates: pd.DataFrame,
    symbol_frames: dict[str, pd.DataFrame],
    rule_map: dict[str, Any],
    profile: ExitProfile,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, group in candidates.groupby("symbol", sort=False):
        df = symbol_frames.get(str(symbol))
        if df is None or df.empty:
            continue
        pos_by_t = {int(t): i for i, t in enumerate(df["t"].to_numpy())}
        for _, cand in group.iterrows():
            rule = rule_map.get(str(cand["rule"]))
            if rule is None:
                continue
            signal_i = pos_by_t.get(int(cand["signal_time"]))
            if signal_i is None:
                continue
            sim = simulate_exit(df, signal_i, rule, profile)
            if sim is None:
                continue
            row = cand.to_dict()
            row.update(sim)
            rows.append(row)
    return pd.DataFrame(rows)


def simulate_exit(df: pd.DataFrame, signal_i: int, rule: Any, profile: ExitProfile) -> dict[str, Any] | None:
    entry_i = signal_i + 1
    if entry_i >= len(df):
        return None
    sig = df.iloc[signal_i]
    entry_row = df.iloc[entry_i]
    entry = float(entry_row["open"])
    if not np.isfinite(entry) or entry <= 0:
        return None
    recent_high = float(df.iloc[max(0, signal_i - rule.break_lookback): signal_i + 1]["body_high"].max())
    stop = min(
        max(float(sig["high"]), recent_high) * (1.0 + profile.stop_body_high_buffer),
        entry * (1.0 + profile.stop_cap),
    )
    best_low = entry
    worst_high = entry
    exit_i = min(entry_i + profile.max_hold_min, len(df) - 1)
    exit_price = float(df.iloc[exit_i]["close"])
    reason = "timeout"
    trailing_active = False
    trailing_low = entry
    trail = 0.0
    for j in range(entry_i, min(len(df), entry_i + profile.max_hold_min + 1)):
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
            if close > prior_low * (1.0 + profile.quick_reclaim_buffer):
                exit_i = j
                exit_price = close
                reason = "quick_reclaim"
                break
        if mfe >= profile.rebound_activate:
            rebound = close / best_low - 1.0
            if rebound >= profile.rebound_retrace:
                exit_i = j
                exit_price = close
                reason = "rebound_trail"
                break
        best_low = min(best_low, low)
        mfe = entry / best_low - 1.0
        if not trailing_active and mfe >= profile.trail_activate:
            trailing_active = True
            trailing_low = best_low
            trail = trailing_low * (1.0 + profile.trail_rebound)
        elif trailing_active:
            trailing_low = min(trailing_low, best_low)
            trail = min(trail, trailing_low * (1.0 + profile.trail_rebound))
    return {
        "entry_time": int(entry_row["t"]),
        "exit_time": int(df.iloc[exit_i]["t"]),
        "entry": entry,
        "exit": exit_price,
        "ret": 1.0 - exit_price / entry - FEE_ROUND_TRIP,
        "mae": worst_high / entry - 1.0,
        "mfe": entry / best_low - 1.0,
        "hold_min": max(1, exit_i - entry_i + 1),
        "exit_reason": reason,
    }


def render_report(summary: pd.DataFrame) -> str:
    lines = ["# Waterfall Exit Profile Search", ""]
    if summary.empty:
        return "# Waterfall Exit Profile Search\n\nNo results.\n"
    for period in ("holdout_202604+", "recent90", "all"):
        part = summary[summary["period"] == period].copy()
        if part.empty:
            continue
        part = part.sort_values(["profit_factor", "avg_ret"], ascending=False).head(30)
        lines.append(f"## {period}")
        lines.append(part.to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", required=True)
    parser.add_argument("--source", default=r"E:\A\bb\data")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out-dir", default="backend/storage/ml/waterfall_exit_profiles")
    parser.add_argument("--rule-include", default="")
    parser.add_argument("--configs", default=",".join(FOCUS_CONFIGS))
    parser.add_argument("--progress-every", type=int, default=25)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
