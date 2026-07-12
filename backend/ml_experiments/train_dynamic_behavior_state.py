"""Train behavior-state lifecycle models.

This experiment replaces fixed hour buckets with live-observable behavior
states. The states are rule-derived from closed 15m candles and lifecycle
context available at each decision bar:

- acceleration: rally still pressing near highs.
- climax_risk: extended rally with topping/failure symptoms.
- distribution: high-level churn after a pump.
- breakdown: high-to-low structure has already broken.
- pullback_risk: fast pullback that is not yet full breakdown.
- trend_hold: healthy rally / hold-long region.
- neutral_watch: no strong behavior state.

The states are not production logic. They are used to test whether dynamic
behavior gates are more useful than fixed 0-8h/8-24h/24-48h buckets.

Example:
    python -m ml_experiments.train_dynamic_behavior_state --targets fast_dump,slow_or_second,short_start,flat_long,continue_long
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn

from ml_experiments.train_event_family_classifier import FAMILY_ORDER
from ml_experiments.train_lifecycle_sequence_models import (
    MODEL_SPECS,
    ModelSpec,
    apply_scaler,
    binary_metrics,
    binary_rank_score,
    build_model,
    chronological_split,
    fit_scaler,
    fit_torch_model,
    load_npz,
    predict_binary,
    seed_everything,
)

TARGETS = {
    "fast_dump": "family == 'fast_dump'",
    "slow_or_second": "family in {'slow_distribution', 'second_distribution'}",
    "short_start": "y_short_start == 1",
    "flat_long": "y_flat_long == 1",
    "continue_long": "y_continue_long == 1",
}

BEHAVIOR_ORDER = [
    "acceleration",
    "climax_risk",
    "distribution",
    "breakdown",
    "pullback_risk",
    "trend_hold",
    "neutral_watch",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    torch.set_num_threads(args.num_threads)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = load_npz(Path(args.dataset))
    states = load_aligned_states(Path(args.states), data["row_id"])
    states["behavior_state"] = assign_behavior_states(states)
    selected_targets = [x.strip() for x in args.targets.split(",") if x.strip()]
    selected_models = [s for s in MODEL_SPECS if s.name in args.models.split(",")]
    if not selected_models:
        raise SystemExit("no selected models")

    results: dict[str, Any] = {
        "dataset": args.dataset,
        "states": args.states,
        "samples": int(len(states)),
        "seq_len": int(data["x"].shape[1]),
        "features": int(data["x"].shape[2]),
        "behavior_order": BEHAVIOR_ORDER,
        "target_definitions": TARGETS,
        "behavior_profile": behavior_profile(states),
        "models": {},
    }

    for target in selected_targets:
        if target not in TARGETS:
            raise SystemExit(f"unknown target: {target}")
        y = make_target(states, target)
        target_result = {
            "positive_rate": float(y.mean()),
            "positives": int(y.sum()),
            "definition": TARGETS[target],
            "models": [],
        }
        for spec in selected_models:
            print(f"training {target} {spec.name}", flush=True)
            run = train_binary_target(spec, data, states, y, target, out_dir / target, args)
            target_result["models"].append(run)
            print(json.dumps(run["summary"], ensure_ascii=False), flush=True)
        target_result["models"].sort(key=lambda r: r["summary"].get("test_rank_score", 0.0), reverse=True)
        results["models"][target] = target_result

    out_json = out_dir / "dynamic_behavior_state.json"
    out_md = out_dir / "dynamic_behavior_state.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train dynamic behavior-state lifecycle models.")
    parser.add_argument("--dataset", default="storage/ml/lifecycle_seq/lifecycle_seq_family.npz")
    parser.add_argument("--states", default="storage/ml/lifecycle/state_rows.parquet")
    parser.add_argument("--out-dir", default="storage/ml/dynamic_behavior_state")
    parser.add_argument("--models", default="gru_stack")
    parser.add_argument("--targets", default="fast_dump,slow_or_second,short_start,flat_long,continue_long")
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--patience", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-threads", type=int, default=2)
    return parser.parse_args(argv)


def load_aligned_states(path: Path, row_ids: np.ndarray) -> pd.DataFrame:
    states = pd.read_parquet(path).copy()
    states["row_id"] = (
        states["symbol"].astype(str)
        + "-"
        + states["entry_time"].astype("int64").astype(str)
        + "-"
        + states["decision_time"].astype("int64").astype(str)
    )
    aligned = states.set_index("row_id").reindex([str(x) for x in row_ids]).reset_index()
    if aligned["symbol"].isna().any():
        missing = int(aligned["symbol"].isna().sum())
        raise SystemExit(f"missing aligned state rows: {missing}")
    return aligned


def make_target(states: pd.DataFrame, target: str) -> np.ndarray:
    if target == "fast_dump":
        y = states["family"].eq("fast_dump")
    elif target == "slow_or_second":
        y = states["family"].isin(["slow_distribution", "second_distribution"])
    elif target == "short_start":
        y = states["y_short_start"].astype(bool)
    elif target == "flat_long":
        y = states["y_flat_long"].astype(bool)
    elif target == "continue_long":
        y = states["y_continue_long"].astype(bool)
    else:
        raise ValueError(target)
    return y.astype(np.float32).to_numpy()


def assign_behavior_states(states: pd.DataFrame) -> np.ndarray:
    ret = states["ctx_ret_since_entry"].to_numpy(float)
    high = states["ctx_high_since_entry"].to_numpy(float)
    drawdown = -states["ctx_drawdown_from_entry_high"].to_numpy(float)
    qv_recent = states["ctx_qv_recent_ratio"].to_numpy(float)
    tsell = states["ctx_taker_sell_mean"].to_numpy(float)
    red = states["ctx_red_bar_share"].to_numpy(float)
    new_high = states["ctx_new_high_since_entry"].to_numpy(float) > 0.5
    ret1 = states["ret_1"].to_numpy(float)
    ret3 = states["ret_3"].to_numpy(float)
    ret6 = states["ret_6"].to_numpy(float)
    uwick = states["uwick"].to_numpy(float)
    close_pos = states["close_pos"].to_numpy(float)
    dist_ema21 = states["dist_ema21"].to_numpy(float)
    volr20 = states["volr_20"].to_numpy(float)

    out = np.full(len(states), "neutral_watch", dtype=object)

    breakdown = (
        (high >= 0.12)
        & (drawdown >= 0.10)
        & ((ret3 <= -0.025) | (ret6 <= -0.04) | (dist_ema21 <= -0.02))
    )
    fast_pullback = (
        (high >= 0.12)
        & (drawdown >= 0.06)
        & (drawdown < 0.14)
        & ((ret1 <= -0.025) | (ret3 <= -0.035))
        & ((qv_recent >= 1.20) | (volr20 >= 1.40))
    )
    acceleration = (
        (ret >= 0.06)
        & (drawdown <= 0.04)
        & ((new_high & (ret3 >= 0.005)) | (ret6 >= 0.035))
        & (close_pos >= 0.55)
    )
    climax = (
        (high >= 0.15)
        & (drawdown <= 0.07)
        & (
            (uwick >= 0.025)
            | ((ret1 <= -0.015) & (qv_recent >= 1.10))
            | ((close_pos <= 0.45) & (volr20 >= 1.50))
        )
    )
    distribution = (
        (high >= 0.12)
        & (drawdown >= 0.035)
        & (drawdown < 0.16)
        & (ret >= -0.03)
        & ((red >= 0.46) | (tsell >= 0.51) | (qv_recent >= 1.40))
    )
    trend_hold = (
        (ret >= 0.06)
        & (drawdown <= 0.055)
        & (ret6 >= -0.02)
        & (dist_ema21 >= -0.015)
    )

    # Order matters: severe structural breaks should override softer states.
    out[trend_hold] = "trend_hold"
    out[distribution] = "distribution"
    out[climax] = "climax_risk"
    out[acceleration] = "acceleration"
    out[fast_pullback] = "pullback_risk"
    out[breakdown] = "breakdown"
    return out


def behavior_profile(states: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for behavior in BEHAVIOR_ORDER:
        grp = states[states["behavior_state"] == behavior]
        if grp.empty:
            out[behavior] = {"rows": 0}
            continue
        out[behavior] = {
            "rows": int(len(grp)),
            "events": int(grp[["symbol", "entry_time"]].drop_duplicates().shape[0]),
            "fast_dump_rate": float(grp["family"].eq("fast_dump").mean()),
            "slow_or_second_rate": float(grp["family"].isin(["slow_distribution", "second_distribution"]).mean()),
            "short_start_rate": float(grp["y_short_start"].mean()),
            "flat_long_rate": float(grp["y_flat_long"].mean()),
            "continue_long_rate": float(grp["y_continue_long"].mean()),
            "median_ret_since_entry": float(grp["ctx_ret_since_entry"].median()),
            "median_high_since_entry": float(grp["ctx_high_since_entry"].median()),
            "median_drawdown_from_high": float((-grp["ctx_drawdown_from_entry_high"]).median()),
            "median_hours_since_entry": float(grp["ctx_hours_since_entry"].median()),
        }
    return out


def train_binary_target(
    spec: ModelSpec,
    data: dict[str, Any],
    states: pd.DataFrame,
    y: np.ndarray,
    target: str,
    out_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    seed_everything(args.seed, target + spec.name)
    x = data["x"].astype(np.float32)
    ts = data["timestamp"].astype(np.int64)
    split = chronological_split(ts)
    train_idx, val_idx, test_idx = split["train"], split["val"], split["test"]
    scaler = fit_scaler(x[train_idx])
    x_train = apply_scaler(x[train_idx], scaler)
    x_val = apply_scaler(x[val_idx], scaler)
    x_test = apply_scaler(x[test_idx], scaler)
    y_train, y_val, y_test = y[train_idx], y[val_idx], y[test_idx]
    model = build_model(spec, x.shape[-1], x.shape[1], output_dim=1)
    pos = float(y_train.sum())
    neg = float(len(y_train) - pos)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([neg / max(pos, 1.0)], dtype=torch.float32))
    model, history = fit_torch_model(model, x_train, y_train, x_val, y_val, loss_fn, args, multiclass=False)
    val_score = predict_binary(model, x_val)
    test_score = predict_binary(model, x_test)
    val = binary_metrics(y_val, val_score)
    test = binary_metrics(y_test, test_score)
    val["by_behavior"] = metrics_by_behavior(y_val, val_score, states.iloc[val_idx]["behavior_state"].to_numpy())
    test["by_behavior"] = metrics_by_behavior(y_test, test_score, states.iloc[test_idx]["behavior_state"].to_numpy())

    run_dir = out_dir / f"{target}_{spec.name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "model.pt"
    torch.save(
        {
            "task": target,
            "model_name": spec.name,
            "model_kind": spec.kind,
            "model_params": spec.params,
            "seq_len": int(x.shape[1]),
            "feature_names": [str(v) for v in data["feature_names"].tolist()],
            "mean": scaler[0],
            "std": scaler[1],
            "behavior_order": BEHAVIOR_ORDER,
            "state_dict": model.state_dict(),
        },
        model_path,
    )
    summary = {
        "target": target,
        "model": spec.name,
        "kind": spec.kind,
        "samples": int(len(x)),
        "positive_rate": float(y.mean()),
        "train_samples": int(len(train_idx)),
        "val_samples": int(len(val_idx)),
        "test_samples": int(len(test_idx)),
        "positive_rate_test": float(y_test.mean()) if len(y_test) else None,
        **{f"val_{k}": v for k, v in val.items()},
        **{f"test_{k}": v for k, v in test.items()},
        "model_path": str(model_path),
    }
    summary["val_rank_score"] = binary_rank_score(summary, "val")
    summary["test_rank_score"] = binary_rank_score(summary, "test")
    (run_dir / "metrics.json").write_text(json.dumps({"summary": summary, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary": summary, "history": history}


def metrics_by_behavior(y: np.ndarray, score: np.ndarray, behavior: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in BEHAVIOR_ORDER:
        mask = behavior == name
        if int(mask.sum()) < 30:
            continue
        out[name] = {
            "rows": int(mask.sum()),
            **binary_metrics(y[mask], score[mask]),
        }
    return out


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Dynamic Behavior State Models",
        "",
        "Behavior-state experiment using live-observable lifecycle states instead of fixed hour buckets.",
        "",
        f"- Dataset: `{results['dataset']}`",
        f"- States: `{results['states']}`",
        f"- Samples: {results['samples']}",
        f"- Sequence length: {results['seq_len']}",
        "",
        "## Behavior Profile",
        "",
        "| Behavior | Rows | Events | Fast Dump | Slow/Second | Short Start | Flat Long | Continue Long | Med Ret | Med High | Med DD | Med Hours |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for behavior in BEHAVIOR_ORDER:
        s = results["behavior_profile"].get(behavior, {})
        lines.append(
            f"| {behavior} | {s.get('rows', 0)} | {s.get('events', 0)} | {pct(s.get('fast_dump_rate'))} | "
            f"{pct(s.get('slow_or_second_rate'))} | {pct(s.get('short_start_rate'))} | "
            f"{pct(s.get('flat_long_rate'))} | {pct(s.get('continue_long_rate'))} | "
            f"{pct(s.get('median_ret_since_entry'))} | {pct(s.get('median_high_since_entry'))} | "
            f"{pct(s.get('median_drawdown_from_high'))} | {num(s.get('median_hours_since_entry'))} |"
        )

    lines += [
        "",
        "## Overall Test Metrics",
        "",
        "| Target | Model | Base | AUC | AP | q90 Precision | q95 Precision | Top5% Precision |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for target, target_result in results["models"].items():
        for item in target_result.get("models", []):
            s = item["summary"]
            lines.append(
                f"| {target} | {s['model']} | {pct(s.get('test_base_rate'))} | {num(s.get('test_auc'))} | "
                f"{num(s.get('test_ap'))} | {pct(s.get('test_q90', {}).get('precision'))} | "
                f"{pct(s.get('test_q95', {}).get('precision'))} | {pct(s.get('test_p_top_5pct'))} |"
            )

    lines += [
        "",
        "## Test Metrics By Behavior",
        "",
        "| Target | Model | Behavior | Rows | Base | AUC | q90 Precision | q95 Precision | Top5% Precision |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for target, target_result in results["models"].items():
        for item in target_result.get("models", []):
            s = item["summary"]
            for behavior in BEHAVIOR_ORDER:
                stats = s.get("test_by_behavior", {}).get(behavior)
                if not stats:
                    continue
                lines.append(
                    f"| {target} | {s['model']} | {behavior} | {stats['rows']} | {pct(stats.get('base_rate'))} | "
                    f"{num(stats.get('auc'))} | {pct(stats.get('q90', {}).get('precision'))} | "
                    f"{pct(stats.get('q95', {}).get('precision'))} | {pct(stats.get('p_top_5pct'))} |"
                )
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
