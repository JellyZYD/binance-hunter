"""Train hierarchical dynamic family classifiers.

This upgrades the previous 5-way family classifier into operational binary
classifiers:

- fast_dump: urgent dump family vs the rest.
- slow_or_second_distribution: slow distribution / second distribution.
- continuation: continuing rally / avoid-short family.

The script trains sequence models directly on each binary target, reports
results by lifecycle stage bucket, and can train stage-specific expert models.
It is experiment-only and does not modify production models.

Example:
    python -m ml_experiments.train_dynamic_family_hierarchical --models gru_medium
    python -m ml_experiments.train_dynamic_family_hierarchical --stage-specific
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
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
    load_lgb_baseline,
    load_npz,
    predict_binary,
    seed_everything,
)

TARGETS = {
    "fast_dump": {"fast_dump"},
    "slow_or_second_distribution": {"slow_distribution", "second_distribution"},
    "continuation": {"continuation"},
}

STAGE_BUCKETS = {
    "early_0_8h": (0.0, 8.0),
    "mid_8_24h": (8.0, 24.0),
    "distribution_24_48h": (24.0, 48.0),
    "late_48_72h": (48.0, 72.0),
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args()
    torch.set_num_threads(args.num_threads)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = load_npz(Path(args.dataset))
    selected = [s for s in MODEL_SPECS if not args.models or s.name in args.models.split(",")]
    if not selected:
        raise SystemExit("no model specs selected")

    results: dict[str, Any] = {
        "dataset": args.dataset,
        "samples": int(len(data["x"])),
        "seq_len": int(data["x"].shape[1]),
        "features": int(data["x"].shape[2]),
        "family_order": FAMILY_ORDER,
        "targets": {k: sorted(v) for k, v in TARGETS.items()},
        "stage_buckets": STAGE_BUCKETS,
        "stage_specific": bool(args.stage_specific),
        "models": {},
        "lgb_baseline": load_lgb_baseline(Path(args.lgb_baseline)),
    }

    for target, families in TARGETS.items():
        y = make_target(data["y_family"], families)
        target_result = {
            "positive_rate": float(y.mean()),
            "positives": int(y.sum()),
            "models": [],
        }
        if int(y.sum()) < args.min_positives:
            target_result["skipped"] = f"positives<{args.min_positives}"
            results["models"][target] = target_result
            continue
        target_data = dict(data)
        target_data[target] = y.astype(np.float32)
        for spec in selected:
            print(f"training {target} {spec.name}", flush=True)
            run = train_binary_target(spec, target_data, target, out_dir / target, args)
            target_result["models"].append(run)
            print(json.dumps(run["summary"], ensure_ascii=False), flush=True)
        target_result["models"].sort(key=lambda x: x["summary"].get("test_rank_score", 0.0), reverse=True)
        if args.stage_specific:
            target_result["stage_specific"] = train_stage_specific_models(target, y, data, selected, out_dir, args)
        results["models"][target] = target_result

    out_json = out_dir / "dynamic_family_hierarchical.json"
    out_md = out_dir / "dynamic_family_hierarchical.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train hierarchical binary family sequence classifiers.")
    parser.add_argument("--dataset", default="storage/ml/lifecycle_seq/lifecycle_seq_family.npz")
    parser.add_argument("--out-dir", default="storage/ml/dynamic_family_hierarchical")
    parser.add_argument("--lgb-baseline", default="storage/ml/lifecycle_models.json")
    parser.add_argument("--models", default="gru_medium")
    parser.add_argument("--epochs", type=int, default=14)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--min-positives", type=int, default=50)
    parser.add_argument("--stage-specific", action="store_true", help="Train separate binary experts inside each lifecycle stage bucket.")
    parser.add_argument("--stage-min-positives", type=int, default=30, help="Minimum total positives required for a stage-specific model.")
    parser.add_argument("--stage-min-eval-positives", type=int, default=3, help="Minimum positives in val/test for useful stage-specific metrics.")
    return parser.parse_args()


def make_target(y_family: np.ndarray, families: set[str]) -> np.ndarray:
    ids = {FAMILY_ORDER.index(f) for f in families}
    return np.isin(y_family.astype(int), list(ids)).astype(np.int8)


def train_stage_specific_models(
    target: str,
    y: np.ndarray,
    data: dict[str, Any],
    selected: list[ModelSpec],
    out_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    stage_specific: dict[str, Any] = {}
    stage = data["stage_hours"].astype(np.float32)
    for stage_name, (lo, hi) in STAGE_BUCKETS.items():
        mask = (stage >= lo) & (stage <= hi)
        stats = split_stats(data["timestamp"][mask].astype(np.int64), y[mask].astype(np.float32))
        stage_result: dict[str, Any] = {
            "range_hours": [lo, hi],
            "samples": int(mask.sum()),
            "positives": int(y[mask].sum()),
            "positive_rate": float(y[mask].mean()) if int(mask.sum()) else None,
            "split_stats": stats,
            "models": [],
        }
        skip_reason = stage_skip_reason(stage_result, args)
        if skip_reason:
            stage_result["skipped"] = skip_reason
            stage_specific[stage_name] = stage_result
            continue
        stage_data = subset_data(data, mask)
        stage_data[target] = y[mask].astype(np.float32)
        for spec in selected:
            print(f"training {target} {stage_name} {spec.name}", flush=True)
            run = train_binary_target(spec, stage_data, target, out_dir / target / f"stage_{stage_name}", args, stage_scope=stage_name)
            stage_result["models"].append(run)
            print(json.dumps(run["summary"], ensure_ascii=False), flush=True)
        stage_result["models"].sort(key=lambda x: x["summary"].get("test_rank_score", 0.0), reverse=True)
        stage_specific[stage_name] = stage_result
    return stage_specific


def subset_data(data: dict[str, Any], mask: np.ndarray) -> dict[str, Any]:
    n = int(len(mask))
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, np.ndarray) and value.shape[:1] == (n,):
            out[key] = value[mask]
        else:
            out[key] = value
    return out


def split_stats(ts: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    if len(ts) == 0:
        return {}
    split = chronological_split(ts)
    out: dict[str, Any] = {}
    for name, idx in split.items():
        yy = y[idx]
        out[name] = {
            "samples": int(len(idx)),
            "positives": int(yy.sum()),
            "positive_rate": float(yy.mean()) if len(yy) else None,
        }
    return out


def stage_skip_reason(stage_result: dict[str, Any], args: argparse.Namespace) -> str | None:
    if int(stage_result["samples"]) < max(args.batch_size, 100):
        return "samples_too_small"
    if int(stage_result["positives"]) < args.stage_min_positives:
        return f"positives<{args.stage_min_positives}"
    stats = stage_result.get("split_stats", {})
    train_pos = stats.get("train", {}).get("positives", 0)
    val_pos = stats.get("val", {}).get("positives", 0)
    test_pos = stats.get("test", {}).get("positives", 0)
    if train_pos < args.stage_min_positives:
        return f"train_positives<{args.stage_min_positives}"
    if val_pos < args.stage_min_eval_positives:
        return f"val_positives<{args.stage_min_eval_positives}"
    if test_pos < args.stage_min_eval_positives:
        return f"test_positives<{args.stage_min_eval_positives}"
    return None


def train_binary_target(
    spec: ModelSpec,
    data: dict[str, Any],
    target: str,
    out_dir: Path,
    args: argparse.Namespace,
    stage_scope: str | None = None,
) -> dict[str, Any]:
    seed_everything(args.seed, target + spec.name)
    x = data["x"].astype(np.float32)
    y = data[target].astype(np.float32)
    ts = data["timestamp"].astype(np.int64)
    stage = data["stage_hours"].astype(np.float32)
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
    val["by_stage"] = stage_metrics(y_val, val_score, stage[val_idx])
    test["by_stage"] = stage_metrics(y_test, test_score, stage[test_idx])

    run_dir = out_dir / f"{target}_{spec.name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "model.pt"
    torch.save(
        {
            "task": target,
            "target_families": sorted(TARGETS[target]),
            "model_name": spec.name,
            "model_kind": spec.kind,
            "model_params": spec.params,
            "seq_len": int(x.shape[1]),
            "feature_names": [str(v) for v in data["feature_names"].tolist()],
            "mean": scaler[0],
            "std": scaler[1],
            "state_dict": model.state_dict(),
        },
        model_path,
    )
    summary = {
        "target": target,
        "stage_scope": stage_scope,
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


def stage_metrics(y: np.ndarray, score: np.ndarray, stage: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (lo, hi) in STAGE_BUCKETS.items():
        mask = (stage >= lo) & (stage <= hi)
        if int(mask.sum()) < 30:
            continue
        out[name] = {
            "rows": int(mask.sum()),
            **binary_metrics(y[mask], score[mask]),
        }
    return out


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Dynamic Family Hierarchical Models",
        "",
        "Binary sequence classifiers for operational dynamic family decisions.",
        "",
        f"- Dataset: `{results['dataset']}`",
        f"- Samples: {results['samples']}",
        f"- Sequence length: {results['seq_len']}",
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
        "## Global Model Metrics By Stage",
        "",
        "| Target | Model | Stage | Rows | Base | AUC | q90 Precision | q95 Precision |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for target, target_result in results["models"].items():
        for item in target_result.get("models", []):
            s = item["summary"]
            for stage, stats in s.get("test_by_stage", {}).items():
                lines.append(
                    f"| {target} | {s['model']} | {stage} | {stats['rows']} | {pct(stats.get('base_rate'))} | "
                    f"{num(stats.get('auc'))} | {pct(stats.get('q90', {}).get('precision'))} | "
                    f"{pct(stats.get('q95', {}).get('precision'))} |"
                )

    if results.get("stage_specific"):
        lines += [
            "",
            "## Stage-Specific Expert Models",
            "",
            "| Target | Stage | Model | Samples | Positives | Test Base | AUC | AP | q90 Precision | q95 Precision | Top5% Precision |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for target, target_result in results["models"].items():
            for stage_name, stage_result in target_result.get("stage_specific", {}).items():
                if stage_result.get("skipped"):
                    lines.append(
                        f"| {target} | {stage_name} | skipped: {stage_result['skipped']} | "
                        f"{stage_result['samples']} | {stage_result['positives']} | - | - | - | - | - | - |"
                    )
                    continue
                for item in stage_result.get("models", []):
                    s = item["summary"]
                    lines.append(
                        f"| {target} | {stage_name} | {s['model']} | {s['samples']} | "
                        f"{int(round(s['positive_rate'] * s['samples']))} | {pct(s.get('test_base_rate'))} | "
                        f"{num(s.get('test_auc'))} | {num(s.get('test_ap'))} | "
                        f"{pct(s.get('test_q90', {}).get('precision'))} | "
                        f"{pct(s.get('test_q95', {}).get('precision'))} | {pct(s.get('test_p_top_5pct'))} |"
                    )

        lines += [
            "",
            "## Suggested Stage Gates",
            "",
            "The gate compares the global binary model's stage metrics against the matching stage-specific expert. ",
            "`watch_only` means the stage is not accurate enough for a hard family decision.",
            "",
            "| Target | Stage | Preferred Model | q95 Precision | Action |",
            "|---|---|---|---:|---|",
        ]
        for row in suggested_stage_gates(results):
            lines.append(
                f"| {row['target']} | {row['stage']} | {row['preferred']} | "
                f"{pct(row['q95_precision'])} | {row['action']} |"
            )

    baseline = results.get("lgb_baseline")
    if baseline:
        fam = baseline.get("family", {}).get("operational", {})
        lines += [
            "",
            "## LGB Baseline",
            "",
            "| Target | Base | AUC | q90 Precision | q95 Precision |",
            "|---|---:|---:|---:|---:|",
        ]
        for target in TARGETS:
            b = fam.get(target)
            if not b:
                continue
            lines.append(
                f"| {target} | {pct(b.get('base_rate'))} | {num(b.get('auc'))} | "
                f"{pct(b.get('thresholds', {}).get('q90', {}).get('precision'))} | "
                f"{pct(b.get('thresholds', {}).get('q95', {}).get('precision'))} |"
            )
    return "\n".join(lines)


def suggested_stage_gates(results: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target, target_result in results["models"].items():
        global_models = target_result.get("models", [])
        global_summary = global_models[0]["summary"] if global_models else {}
        stage_specific = target_result.get("stage_specific", {})
        for stage_name in STAGE_BUCKETS:
            global_stats = global_summary.get("test_by_stage", {}).get(stage_name, {})
            global_q95 = nested_precision(global_stats)
            stage_result = stage_specific.get(stage_name, {})
            stage_q95 = None
            if not stage_result.get("skipped") and stage_result.get("models"):
                stage_q95 = stage_result["models"][0]["summary"].get("test_q95", {}).get("precision")
            preferred = "global"
            best_q95 = global_q95
            if stage_q95 is not None and (best_q95 is None or stage_q95 > best_q95):
                preferred = "stage_specific"
                best_q95 = stage_q95
            rows.append(
                {
                    "target": target,
                    "stage": stage_name,
                    "preferred": preferred if target != "continuation" else "do_not_use",
                    "q95_precision": best_q95,
                    "action": suggested_action(target, stage_name, best_q95),
                }
            )
    return rows


def nested_precision(stats: dict[str, Any]) -> float | None:
    value = stats.get("q95", {}).get("precision")
    return float(value) if value is not None else None


def suggested_action(target: str, stage: str, q95: float | None) -> str:
    if target == "continuation":
        return "do not use as hard continuation signal"
    if q95 is None:
        return "insufficient data"
    if stage == "early_0_8h":
        if target == "fast_dump" and q95 >= 0.70:
            return "risk warning only; no hard family lock"
        return "watch_only"
    if target == "fast_dump" and q95 >= 0.80:
        return "high-conf fast-dump path"
    if target == "slow_or_second_distribution" and q95 >= 0.65:
        return "distribution path confirmation"
    if q95 >= 0.60:
        return "medium confidence observation"
    return "watch_only"


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
