"""Optimize dynamic behavior-state thresholds for lifecycle routing.

This script evaluates the router itself before wiring it into a full signal
stack. For each threshold configuration it reassigns behavior states from
closed 15m lifecycle rows, then measures the first lifecycle bar that enters
the operational gates:

- fast top: distribution/climax/pullback
- fast short: distribution/climax/pullback or breakdown/pullback
- slow short: distribution/pullback or breakdown/pullback

The evaluation uses labels only for scoring; the router inputs are all
past/current closed-candle fields.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_experiments.train_dense_lifecycle_experts import BEHAVIOR_ORDER, dynamic_task_labels, fixed_task_labels
from ml_experiments.train_dynamic_behavior_state import assign_behavior_states as assign_fixed_states
from ml_experiments.train_slow_distribution_experts import RECIPES, add_slow_features, make_task_frame


@dataclass(frozen=True)
class RouterConfig:
    name: str
    min_high_floor: float
    high_noise_mult: float
    pull_amp: float
    pull_noise: float
    break_amp: float
    break_noise: float
    dist_min_amp: float
    dist_min_noise: float
    dist_max_amp: float
    dist_max_noise: float
    climax_min_amp: float
    climax_noise: float
    ret_noise: float


CONFIGS = [
    RouterConfig("dyn_balanced", 0.10, 2.2, 0.10, 1.3, 0.18, 2.0, 0.055, 0.8, 0.28, 2.2, 0.13, 1.2, 1.2),
    RouterConfig("dyn_sensitive", 0.08, 1.8, 0.08, 1.0, 0.14, 1.6, 0.040, 0.6, 0.24, 1.8, 0.11, 1.0, 1.0),
    RouterConfig("dyn_strict", 0.12, 2.6, 0.12, 1.6, 0.22, 2.4, 0.065, 1.0, 0.32, 2.6, 0.16, 1.4, 1.4),
    RouterConfig("dyn_big_pump_tolerant", 0.10, 2.0, 0.14, 1.3, 0.26, 2.0, 0.050, 0.8, 0.36, 2.3, 0.14, 1.2, 1.2),
    RouterConfig("dyn_low_adverse", 0.11, 2.4, 0.11, 1.5, 0.20, 2.5, 0.060, 1.0, 0.26, 2.0, 0.14, 1.5, 1.5),
    RouterConfig("dyn_early_distribution", 0.09, 1.8, 0.09, 1.2, 0.18, 1.9, 0.030, 0.5, 0.30, 2.0, 0.12, 1.1, 1.1),
]

GATES = {
    "distribution_climax_pullback": {"distribution", "climax_risk", "pullback_risk"},
    "breakdown_pullback": {"breakdown", "pullback_risk"},
    "distribution_zone": {"distribution", "climax_risk", "trend_hold"},
    "all_risk": {"distribution", "climax_risk", "pullback_risk", "breakdown"},
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dense = pd.read_parquet(args.dataset).copy()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "dataset": args.dataset,
        "configs": [{"name": "fixed", "type": "fixed"}] + [asdict(c) for c in CONFIGS],
        "runs": [],
        "note": "Router-only first-state-gate optimization; no production logic changed.",
    }
    for name, states in [("fixed", assign_fixed_states(dense))] + [(cfg.name, assign_dynamic_states(dense, cfg)) for cfg in CONFIGS]:
        print(f"evaluating router {name}", flush=True)
        routed = dense.copy()
        routed["behavior_state"] = states
        for behavior in BEHAVIOR_ORDER:
            routed[f"behavior_{behavior}"] = (routed["behavior_state"] == behavior).astype("int8")
        run = evaluate_router(routed, name)
        results["runs"].append(run)
        print(json.dumps(run_summary(run), ensure_ascii=False), flush=True)

    results["runs"].sort(key=lambda r: r["score"], reverse=True)
    out_json = out_dir / "dynamic_router_thresholds.json"
    out_md = out_dir / "dynamic_router_thresholds.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize dynamic behavior-router thresholds.")
    parser.add_argument("--dataset", default="storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--out-dir", default="storage/ml/dynamic_router_thresholds")
    return parser.parse_args(argv)


def assign_dynamic_states(rows: pd.DataFrame, cfg: RouterConfig) -> np.ndarray:
    ret = pd.to_numeric(rows["ctx_ret_since_entry"], errors="coerce").fillna(0.0).to_numpy(float)
    high = pd.to_numeric(rows["ctx_high_since_entry"], errors="coerce").fillna(0.0).clip(lower=0.0).to_numpy(float)
    drawdown = (-pd.to_numeric(rows["ctx_drawdown_from_entry_high"], errors="coerce").fillna(0.0)).clip(lower=0.0).to_numpy(float)
    qv_recent = pd.to_numeric(rows["ctx_qv_recent_ratio"], errors="coerce").fillna(1.0).to_numpy(float)
    tsell = pd.to_numeric(rows["ctx_taker_sell_mean"], errors="coerce").fillna(0.5).to_numpy(float)
    red = pd.to_numeric(rows["ctx_red_bar_share"], errors="coerce").fillna(0.0).to_numpy(float)
    new_high = pd.to_numeric(rows["ctx_new_high_since_entry"], errors="coerce").fillna(0.0).to_numpy(float) > 0.5
    ret1 = pd.to_numeric(rows["ret_1"], errors="coerce").fillna(0.0).to_numpy(float)
    ret3 = pd.to_numeric(rows["ret_3"], errors="coerce").fillna(0.0).to_numpy(float)
    ret6 = pd.to_numeric(rows["ret_6"], errors="coerce").fillna(0.0).to_numpy(float)
    uwick = pd.to_numeric(rows["uwick"], errors="coerce").fillna(0.0).to_numpy(float)
    close_pos = pd.to_numeric(rows["close_pos"], errors="coerce").fillna(0.5).to_numpy(float)
    dist_ema21 = pd.to_numeric(rows["dist_ema21"], errors="coerce").fillna(0.0).to_numpy(float)
    volr20 = pd.to_numeric(rows["volr_20"], errors="coerce").fillna(1.0).to_numpy(float)
    noise = router_noise(rows).to_numpy(float)

    min_high = np.maximum(cfg.min_high_floor, cfg.high_noise_mult * noise)
    pull_min = np.clip(np.maximum(0.045, cfg.pull_amp * high + cfg.pull_noise * noise), 0.045, 0.18)
    break_min = np.clip(np.maximum(0.08, cfg.break_amp * high + cfg.break_noise * noise), 0.08, 0.28)
    dist_min = np.clip(np.maximum(0.025, cfg.dist_min_amp * high + cfg.dist_min_noise * noise), 0.025, 0.10)
    dist_max = np.clip(np.maximum(0.12, cfg.dist_max_amp * high + cfg.dist_max_noise * noise), 0.12, 0.34)
    climax_amp = np.maximum(cfg.climax_min_amp, cfg.climax_noise * noise)
    ret3_break = -np.maximum(0.020, cfg.ret_noise * noise)
    ret6_break = -np.maximum(0.035, 1.4 * cfg.ret_noise * noise)
    ret1_pull = -np.maximum(0.018, cfg.ret_noise * noise)
    ret3_pull = -np.maximum(0.028, 1.25 * cfg.ret_noise * noise)
    ema_break = -np.maximum(0.015, 0.8 * cfg.ret_noise * noise)
    uwick_min = np.maximum(0.018, 1.2 * noise)

    out = np.full(len(rows), "neutral_watch", dtype=object)
    breakdown = (high >= min_high) & (drawdown >= break_min) & ((ret3 <= ret3_break) | (ret6 <= ret6_break) | (dist_ema21 <= ema_break))
    fast_pullback = (
        (high >= min_high)
        & (drawdown >= pull_min)
        & (drawdown < np.maximum(break_min, pull_min + 0.02))
        & ((ret1 <= ret1_pull) | (ret3 <= ret3_pull))
        & ((qv_recent >= 1.15) | (volr20 >= 1.30))
    )
    acceleration = (
        (ret >= np.maximum(0.045, 1.8 * noise))
        & (drawdown <= np.maximum(0.035, 1.2 * noise))
        & ((new_high & (ret3 >= -0.2 * noise)) | (ret6 >= np.maximum(0.025, 1.2 * noise)))
        & (close_pos >= 0.55)
    )
    climax = (
        (high >= climax_amp)
        & (drawdown <= np.maximum(0.075, 2.0 * noise))
        & ((uwick >= uwick_min) | ((ret1 <= -0.8 * noise) & (qv_recent >= 1.10)) | ((close_pos <= 0.45) & (volr20 >= 1.45)))
    )
    distribution = (
        (high >= min_high)
        & (drawdown >= dist_min)
        & (drawdown < dist_max)
        & (ret >= -np.maximum(0.04, 1.5 * noise))
        & ((red >= 0.46) | (tsell >= 0.51) | (qv_recent >= 1.35))
    )
    trend_hold = (
        (ret >= np.maximum(0.045, 1.8 * noise))
        & (drawdown <= np.maximum(0.055, 1.6 * noise))
        & (ret6 >= -np.maximum(0.018, 0.8 * noise))
        & (dist_ema21 >= -np.maximum(0.012, 0.6 * noise))
    )

    out[trend_hold] = "trend_hold"
    out[distribution] = "distribution"
    out[climax] = "climax_risk"
    out[acceleration] = "acceleration"
    out[fast_pullback] = "pullback_risk"
    out[breakdown] = "breakdown"
    return out


def router_noise(rows: pd.DataFrame) -> pd.Series:
    atr = pd.to_numeric(rows["atr_14"], errors="coerce").fillna(0.0)
    retstd = pd.to_numeric(rows["retstd_20"], errors="coerce").fillna(0.0) * 1.5
    return pd.Series(np.clip(np.maximum(atr.to_numpy(), retstd.to_numpy()), 0.006, 0.10), index=rows.index)


def evaluate_router(dense: pd.DataFrame, name: str) -> dict[str, Any]:
    fast = dense[dense["family"] == "fast_dump"].copy()
    fast_top = frame_from_label(fast, *dynamic_task_labels(fast, "fast_dump", "top_exit"), task="fast_top")
    fast_short = frame_from_label(fast, *dynamic_task_labels(fast, "fast_dump", "short_clean"), task="fast_short")
    slow = add_slow_features(dense[dense["family"].isin(["slow_distribution", "second_distribution"])].copy())
    late_break = next(r for r in RECIPES if r.name == "late_break")
    slow_short = make_task_frame(slow, "breakdown_short", late_break)

    metrics = {
        "state_counts": {str(k): int(v) for k, v in dense["behavior_state"].value_counts().to_dict().items()},
        "fast_top": {
            "distribution_climax_pullback": first_gate_metrics(fast_top, GATES["distribution_climax_pullback"], "top"),
            "all_risk": first_gate_metrics(fast_top, GATES["all_risk"], "top"),
        },
        "fast_short": {
            "distribution_climax_pullback": first_gate_metrics(fast_short, GATES["distribution_climax_pullback"], "short"),
            "breakdown_pullback": first_gate_metrics(fast_short, GATES["breakdown_pullback"], "short"),
            "all_risk": first_gate_metrics(fast_short, GATES["all_risk"], "short"),
        },
        "slow_short": {
            "distribution_climax_pullback": first_gate_metrics(slow_short, GATES["distribution_climax_pullback"], "short"),
            "breakdown_pullback": first_gate_metrics(slow_short, GATES["breakdown_pullback"], "short"),
            "all_risk": first_gate_metrics(slow_short, GATES["all_risk"], "short"),
        },
    }
    score = router_score(metrics)
    return {"name": name, "score": score, **metrics}


def frame_from_label(rows: pd.DataFrame, eligible: pd.Series, target: pd.Series, label_cols: dict[str, pd.Series], task: str) -> pd.DataFrame:
    out = rows[eligible].copy()
    out["target"] = target.loc[out.index].astype("int8")
    out["router_task"] = task
    for name, values in label_cols.items():
        out[name] = values.loc[out.index]
    return out


def first_gate_metrics(rows: pd.DataFrame, states: set[str], kind: str) -> dict[str, Any]:
    if rows.empty:
        return {"lifecycles": 0, "triggered": 0, "coverage": 0.0}
    total = rows[["symbol", "entry_time"]].drop_duplicates().shape[0]
    selected = rows[rows["behavior_state"].isin(states)].sort_values(["symbol", "entry_time", "decision_time"])
    first = selected.groupby(["symbol", "entry_time"], as_index=False).first()
    if first.empty:
        return {"lifecycles": int(total), "triggered": 0, "coverage": 0.0}
    y = first["target"].astype(int).to_numpy()
    out = {
        "lifecycles": int(total),
        "triggered": int(len(first)),
        "coverage": float(len(first) / total) if total else None,
        "precision": float(y.mean()) if len(y) else None,
        "median_delay_hours": median((first["decision_time"] - first["entry_time"]) / 3_600_000),
        "median_future_up_24h": median(first["future_up_24h"]),
        "median_future_drop_6h": median(first["future_drop_6h"]),
        "median_future_drop_12h": median(first["future_drop_12h"]),
        "median_future_drop_24h": median(first["future_drop_24h"]),
        "median_future_drop_72h": median(first["future_drop_72h"]),
        "behavior_mix": {str(k): int(v) for k, v in first["behavior_state"].value_counts().to_dict().items()},
    }
    if kind == "short":
        out["median_short_adverse_6h"] = median(first["short_adverse_before_down5_6h"])
        out["median_short_adverse_24h"] = median(first["short_adverse_before_down5_24h"])
    return out


def median(values: Any) -> float | None:
    s = pd.to_numeric(values, errors="coerce")
    if len(s) == 0:
        return None
    value = float(np.nanmedian(s))
    return value if np.isfinite(value) else None


def router_score(metrics: dict[str, Any]) -> float:
    fast_top = metrics["fast_top"]["distribution_climax_pullback"]
    fast_short = metrics["fast_short"]["distribution_climax_pullback"]
    slow_short = metrics["slow_short"]["distribution_climax_pullback"]
    return 0.35 * top_score(fast_top) + 0.35 * short_score(fast_short) + 0.30 * short_score(slow_short)


def top_score(m: dict[str, Any]) -> float:
    if not m.get("triggered"):
        return -10.0
    return 1.8 * (m.get("precision") or 0.0) + 0.7 * (m.get("coverage") or 0.0) + 1.4 * (m.get("median_future_drop_24h") or 0.0) - 1.4 * (m.get("median_future_up_24h") or 0.0)


def short_score(m: dict[str, Any]) -> float:
    if not m.get("triggered"):
        return -10.0
    return 1.8 * (m.get("precision") or 0.0) + 0.7 * (m.get("coverage") or 0.0) + 1.6 * (m.get("median_future_drop_24h") or 0.0) - 2.2 * (m.get("median_short_adverse_24h") or 0.0)


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": run["name"],
        "score": run["score"],
        "fast_top": pick(run["fast_top"]["distribution_climax_pullback"]),
        "fast_short": pick(run["fast_short"]["distribution_climax_pullback"]),
        "slow_short": pick(run["slow_short"]["distribution_climax_pullback"]),
    }


def pick(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "coverage": m.get("coverage"),
        "precision": m.get("precision"),
        "up24": m.get("median_future_up_24h"),
        "drop24": m.get("median_future_drop_24h"),
        "adv24": m.get("median_short_adverse_24h"),
    }


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Dynamic Router Threshold Optimization",
        "",
        f"- Dataset: `{results['dataset']}`",
        "",
        "## Ranked Configs",
        "",
        "| Config | Score | Fast Top Cov | Fast Top Prec | Fast Top Up24 | Fast Top Drop24 | Fast Short Cov | Fast Short Prec | Fast Short Adv24 | Fast Short Drop24 | Slow Short Cov | Slow Short Prec | Slow Short Adv24 | Slow Short Drop24 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in results["runs"]:
        ft = run["fast_top"]["distribution_climax_pullback"]
        fs = run["fast_short"]["distribution_climax_pullback"]
        ss = run["slow_short"]["distribution_climax_pullback"]
        lines.append(
            f"| {run['name']} | {num(run['score'])} | {pct(ft.get('coverage'))} | {pct(ft.get('precision'))} | "
            f"{pct(ft.get('median_future_up_24h'))} | {pct(ft.get('median_future_drop_24h'))} | "
            f"{pct(fs.get('coverage'))} | {pct(fs.get('precision'))} | {pct(fs.get('median_short_adverse_24h'))} | {pct(fs.get('median_future_drop_24h'))} | "
            f"{pct(ss.get('coverage'))} | {pct(ss.get('precision'))} | {pct(ss.get('median_short_adverse_24h'))} | {pct(ss.get('median_future_drop_24h'))} |"
        )
    lines += [
        "",
        "## State Counts",
        "",
        "| Config | State Counts |",
        "|---|---|",
    ]
    for run in results["runs"]:
        lines.append(f"| {run['name']} | `{json.dumps(run['state_counts'], ensure_ascii=False)}` |")
    return "\n".join(lines)


def pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
