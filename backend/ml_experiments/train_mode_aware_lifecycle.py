"""Train mode-aware lifecycle models.

This experiment implements the mode-aware framework:

1. Entry model: from the long-entry moment, predict whether the lifecycle will
   become one of the target manipulation modes that are useful for long->short
   trading.
2. Dynamic router: as closed 15m candles evolve, estimate which control mode the
   lifecycle most resembles.
3. Per-mode expert models: train separate flat-long and short-start signals for
   each mode, judged by adverse move, near-term drop, and lifecycle coverage.

This is experiment-only and does not modify production signal logic.

Example:
    python -m ml_experiments.train_mode_aware_lifecycle --epochs 8 --patience 3
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import balanced_accuracy_score, log_loss
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from ml_experiments.train_dynamic_behavior_state import BEHAVIOR_ORDER, assign_behavior_states
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
    maybe_ap,
    maybe_auc,
    predict_binary,
    predict_multiclass,
    seed_everything,
)

TARGET_MODES = ["fast_dump", "slow_distribution", "second_distribution"]
ROUTER_ORDER = ["fast_dump", "slow_distribution", "second_distribution", "other"]
DAY_MS = 86_400_000

META_COLS = {
    "symbol",
    "entry_time",
    "decision_time",
    "decision_time_iso",
    "entry_price",
    "current_price",
    "family",
    "cluster",
    "linked_event_id",
    "future_up24",
    "future_drop72",
    "future_drop12",
    "short_adverse_before5",
    "short_minutes_to_down5",
    "y_flat_long",
    "y_short_start",
    "y_continue_long",
    "row_id",
    "behavior_state",
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    torch.set_num_threads(args.num_threads)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entry_seq = load_npz(Path(args.entry_seq))
    family_seq = load_npz(Path(args.family_seq))
    entries = load_aligned_entries(Path(args.entries), entry_seq["row_id"])
    states = load_aligned_states(Path(args.states), family_seq["row_id"])
    states["behavior_state"] = assign_behavior_states(states)

    selected_spec = next((s for s in MODEL_SPECS if s.name == args.model), None)
    if selected_spec is None:
        raise SystemExit(f"unknown model: {args.model}")

    results: dict[str, Any] = {
        "entry_seq": args.entry_seq,
        "family_seq": args.family_seq,
        "entries": args.entries,
        "states": args.states,
        "target_modes": TARGET_MODES,
        "router_order": ROUTER_ORDER,
        "model": args.model,
        "entry_profile": entry_profile(entries),
        "state_profile": state_profile(states),
        "entry_models": {},
        "router": {},
        "experts": {},
    }

    print("training target_mode_entry", flush=True)
    entry_target = entries["family"].isin(TARGET_MODES).astype(np.float32).to_numpy()
    results["entry_models"]["target_mode_entry"] = train_entry_binary(selected_spec, entry_seq, entries, entry_target, "target_mode_entry", out_dir, args)

    print("training quality_target_mode_entry", flush=True)
    quality_target = (entries["family"].isin(TARGET_MODES) & (entries["y_long_start"].astype(int) == 1)).astype(np.float32).to_numpy()
    results["entry_models"]["quality_target_mode_entry"] = train_entry_binary(selected_spec, entry_seq, entries, quality_target, "quality_target_mode_entry", out_dir, args)

    print("training dynamic_router", flush=True)
    results["router"] = train_router(selected_spec, family_seq, states, out_dir / "router", args)

    feature_cols = state_feature_columns(states)
    results["expert_feature_count"] = len(feature_cols)
    for mode in TARGET_MODES:
        results["experts"][mode] = {}
        mode_rows = states[states["family"] == mode].copy()
        print(f"training experts {mode} rows={len(mode_rows)}", flush=True)
        results["experts"][mode]["profile"] = mode_state_profile(mode_rows)
        results["experts"][mode]["top_exit"] = train_lgb_expert(mode_rows, mode, "top_exit", feature_cols, out_dir / "experts" / mode, args)
        results["experts"][mode]["short_clean"] = train_lgb_expert(mode_rows, mode, "short_clean", feature_cols, out_dir / "experts" / mode, args)

    out_json = out_dir / "mode_aware_lifecycle.json"
    out_md = out_dir / "mode_aware_lifecycle.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train mode-aware lifecycle experiment.")
    parser.add_argument("--entry-seq", default="storage/ml/lifecycle_seq/lifecycle_seq_entry.npz")
    parser.add_argument("--family-seq", default="storage/ml/lifecycle_seq/lifecycle_seq_family.npz")
    parser.add_argument("--entries", default="storage/ml/lifecycle/long_entries.parquet")
    parser.add_argument("--states", default="storage/ml/lifecycle/state_rows.parquet")
    parser.add_argument("--out-dir", default="storage/ml/mode_aware_lifecycle")
    parser.add_argument("--model", default="gru_stack")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--min-rows", type=int, default=120)
    parser.add_argument("--min-positives", type=int, default=20)
    return parser.parse_args(argv)


def load_aligned_entries(path: Path, row_ids: np.ndarray) -> pd.DataFrame:
    entries = pd.read_parquet(path).copy()
    entries["row_id"] = entries["symbol"].astype(str) + "-" + entries["entry_time"].astype("int64").astype(str)
    aligned = entries.set_index("row_id").reindex([str(x) for x in row_ids]).reset_index()
    if aligned["symbol"].isna().any():
        raise SystemExit(f"missing aligned entry rows: {int(aligned['symbol'].isna().sum())}")
    return aligned


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
        raise SystemExit(f"missing aligned state rows: {int(aligned['symbol'].isna().sum())}")
    return aligned


def train_entry_binary(
    spec: ModelSpec,
    data: dict[str, Any],
    entries: pd.DataFrame,
    y: np.ndarray,
    target: str,
    out_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    seed_everything(args.seed, target + spec.name)
    x = data["x"].astype(np.float32)
    ts = data["timestamp"].astype(np.int64)
    split = chronological_split(ts)
    scaler = fit_scaler(x[split["train"]])
    x_train = apply_scaler(x[split["train"]], scaler)
    x_val = apply_scaler(x[split["val"]], scaler)
    x_test = apply_scaler(x[split["test"]], scaler)
    y_train, y_val, y_test = y[split["train"]], y[split["val"]], y[split["test"]]
    model = build_model(spec, x.shape[-1], x.shape[1], output_dim=1)
    pos = float(y_train.sum())
    neg = float(len(y_train) - pos)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([neg / max(pos, 1.0)], dtype=torch.float32))
    model, history = fit_torch_model(model, x_train, y_train, x_val, y_val, loss_fn, args, multiclass=False)
    val_score = predict_binary(model, x_val)
    test_score = predict_binary(model, x_test)
    val = binary_metrics(y_val, val_score)
    test = binary_metrics(y_test, test_score)
    test["top_buckets"] = entry_bucket_metrics(entries.iloc[split["test"]], y_test, test_score)
    val["top_buckets"] = entry_bucket_metrics(entries.iloc[split["val"]], y_val, val_score)

    run_dir = out_dir / "entry" / target
    run_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "model.pt"
    torch.save(
        {
            "task": target,
            "target_modes": TARGET_MODES,
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
        "model": spec.name,
        "samples": int(len(x)),
        "positive_rate": float(y.mean()),
        "train_samples": int(len(y_train)),
        "val_samples": int(len(y_val)),
        "test_samples": int(len(y_test)),
        "positive_rate_test": float(y_test.mean()) if len(y_test) else None,
        **{f"val_{k}": v for k, v in val.items()},
        **{f"test_{k}": v for k, v in test.items()},
        "model_path": str(model_path),
    }
    summary["val_rank_score"] = binary_rank_score(summary, "val")
    summary["test_rank_score"] = binary_rank_score(summary, "test")
    (run_dir / "metrics.json").write_text(json.dumps({"summary": summary, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary": summary, "history": history}


def entry_bucket_metrics(rows: pd.DataFrame, y: np.ndarray, score: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for q in (0.80, 0.90, 0.95):
        threshold = float(np.quantile(score, q)) if len(score) else float("nan")
        selected = score >= threshold
        grp = rows.iloc[np.flatnonzero(selected)]
        families = grp["family"].fillna("none").value_counts().to_dict()
        out[f"q{int(q * 100)}"] = {
            "threshold": round(threshold, 6) if np.isfinite(threshold) else None,
            "selected": int(selected.sum()),
            "precision": float(y[selected].mean()) if int(selected.sum()) else None,
            "family_mix": {str(k): int(v) for k, v in families.items()},
            "median_future_high_48h": safe_median(grp, "future_high_48h"),
            "median_adverse_before_up5": safe_median(grp, "adverse_before_up5"),
            "median_minutes_to_up5": safe_median(grp, "minutes_to_up5"),
        }
    return out


def train_router(spec: ModelSpec, data: dict[str, Any], states: pd.DataFrame, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    seed_everything(args.seed, "mode_router" + spec.name)
    label = states["family"].map(lambda x: x if x in TARGET_MODES else "other")
    y = label.map({v: i for i, v in enumerate(ROUTER_ORDER)}).astype(int).to_numpy()
    x = data["x"].astype(np.float32)
    ts = data["timestamp"].astype(np.int64)
    split = chronological_split(ts)
    scaler = fit_scaler(x[split["train"]])
    x_train = apply_scaler(x[split["train"]], scaler)
    x_val = apply_scaler(x[split["val"]], scaler)
    x_test = apply_scaler(x[split["test"]], scaler)
    y_train, y_val, y_test = y[split["train"]], y[split["val"]], y[split["test"]]
    model = build_model(spec, x.shape[-1], x.shape[1], output_dim=len(ROUTER_ORDER))
    counts = np.bincount(y_train, minlength=len(ROUTER_ORDER)).astype(np.float32)
    weights = counts.sum() / np.maximum(counts, 1.0)
    weights = weights / weights.mean()
    loss_fn = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32))
    model, history = fit_router_model(model, x_train, y_train, x_val, y_val, loss_fn, args)
    val_prob = predict_multiclass(model, x_val)
    test_prob = predict_multiclass(model, x_test)
    val = router_metrics(y_val, val_prob, states.iloc[split["val"]])
    test = router_metrics(y_test, test_prob, states.iloc[split["test"]])

    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.pt"
    torch.save(
        {
            "task": "mode_router",
            "router_order": ROUTER_ORDER,
            "target_modes": TARGET_MODES,
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
        "model": spec.name,
        "samples": int(len(x)),
        "class_counts": {ROUTER_ORDER[int(k)]: int(v) for k, v in zip(*np.unique(y, return_counts=True))},
        "train_samples": int(len(y_train)),
        "val_samples": int(len(y_val)),
        "test_samples": int(len(y_test)),
        **{f"val_{k}": v for k, v in val.items()},
        **{f"test_{k}": v for k, v in test.items()},
        "model_path": str(model_path),
    }
    (out_dir / "metrics.json").write_text(json.dumps({"summary": summary, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary": summary, "history": history}


def fit_router_model(
    model: nn.Module,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    loss_fn: nn.Module,
    args: argparse.Namespace,
) -> tuple[nn.Module, list[dict[str, Any]]]:
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    train_loader = DataLoader(
        TensorDataset(torch.from_numpy(x_train), torch.from_numpy(y_train.astype(np.int64))),
        batch_size=args.batch_size,
        shuffle=True,
    )
    val_x = torch.from_numpy(x_val)
    best_score = -float("inf")
    best_state = None
    stale = 0
    history: list[dict[str, Any]] = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        for xb, yb in train_loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(xb)
            loss = loss_fn(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
            total_loss += float(loss.detach()) * len(xb)
        model.eval()
        with torch.no_grad():
            prob = torch.softmax(model(val_x), dim=1).cpu().numpy() if len(x_val) else np.empty((0, len(ROUTER_ORDER)))
        metrics = router_metrics(y_val, prob, pd.DataFrame({"behavior_state": ["neutral_watch"] * len(y_val)}))
        score = router_rank_score(metrics)
        history.append({"epoch": epoch, "train_loss": total_loss / max(len(x_train), 1), "val_score": score, **metrics})
        if score > best_score:
            best_score = score
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
        if stale >= args.patience:
            break
    if best_state is not None:
        model.load_state_dict(best_state)
    return model, history


def router_rank_score(metrics: dict[str, Any]) -> float:
    modes = metrics.get("modes", {})
    fast = modes.get("fast_dump", {})
    slow = modes.get("slow_distribution", {})
    second = modes.get("second_distribution", {})
    return float(
        0.25 * nz(fast.get("auc"))
        + 0.20 * nz(fast.get("q90", {}).get("precision"))
        + 0.20 * nz(slow.get("auc"))
        + 0.15 * nz(slow.get("q90", {}).get("precision"))
        + 0.10 * nz(second.get("auc"))
        + 0.10 * nz(metrics.get("balanced_accuracy"))
    )


def nz(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return 0.0
    return out if np.isfinite(out) else 0.0


def router_metrics(y: np.ndarray, prob: np.ndarray, rows: pd.DataFrame) -> dict[str, Any]:
    pred = np.argmax(prob, axis=1) if len(prob) else np.array([], dtype=np.int64)
    out: dict[str, Any] = {
        "accuracy": float((pred == y).mean()) if len(y) else None,
        "balanced_accuracy": float(balanced_accuracy_score(y, pred)) if len(np.unique(y)) > 1 else None,
        "log_loss": safe_log_loss(y, prob),
        "modes": {},
        "by_behavior": {},
    }
    for mode, idx in zip(ROUTER_ORDER, range(len(ROUTER_ORDER))):
        yy = (y == idx).astype(np.int8)
        score = prob[:, idx]
        out["modes"][mode] = {
            "base_rate": float(yy.mean()) if len(yy) else None,
            "auc": maybe_auc(yy, score),
            "ap": maybe_ap(yy, score),
            "q90": threshold_payload(yy, score, 0.90),
            "q95": threshold_payload(yy, score, 0.95),
        }
    behavior = rows["behavior_state"].to_numpy()
    for name in BEHAVIOR_ORDER:
        mask = behavior == name
        if int(mask.sum()) < 30:
            continue
        out["by_behavior"][name] = {
            "rows": int(mask.sum()),
            "accuracy": float((pred[mask] == y[mask]).mean()),
            "balanced_accuracy": float(balanced_accuracy_score(y[mask], pred[mask])) if len(np.unique(y[mask])) > 1 else None,
        }
    return out


def train_lgb_expert(
    rows: pd.DataFrame,
    mode: str,
    task: str,
    feature_cols: list[str],
    out_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    data = expert_dataset(rows, task).copy()
    if len(data) < args.min_rows:
        return {"skipped": "rows_too_small", "rows": int(len(data))}
    positives = int(data["target"].sum())
    if positives < args.min_positives:
        return {"skipped": f"positives<{args.min_positives}", "rows": int(len(data)), "positives": positives}
    split = time_split(data["decision_time"].astype("int64").to_numpy())
    train = data.iloc[split["train"]]
    val = data.iloc[split["val"]]
    test = data.iloc[split["test"]]
    pos = int(train["target"].sum())
    neg = int(len(train) - pos)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=350,
        learning_rate=0.035,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=2.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model.fit(train[feature_cols], train["target"])
    val_score = model.predict_proba(val[feature_cols])[:, 1] if len(val) else np.array([])
    test_score = model.predict_proba(test[feature_cols])[:, 1] if len(test) else np.array([])
    result = {
        "mode": mode,
        "task": task,
        "rows": int(len(data)),
        "positives": positives,
        "positive_rate": float(data["target"].mean()),
        "split": split_summary(data, split),
        "val": expert_metrics(val, val_score),
        "test": expert_metrics(test, test_score),
        "feature_importance": feature_importance(model, feature_cols),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / f"{task}.txt"
    model.booster_.save_model(str(model_path))
    result["model_path"] = str(model_path)
    (out_dir / f"{task}_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def expert_dataset(rows: pd.DataFrame, task: str) -> pd.DataFrame:
    data = rows.copy()
    if task == "top_exit":
        eligible = (data["ctx_high_since_entry"] >= 0.08) & (data["ctx_ret_since_entry"] >= 0.03)
        target = (
            (data["future_drop72"] >= 0.10)
            & (data["future_up24"] <= 0.04)
            & (data["ctx_ret_since_entry"] >= 0.04)
        )
    elif task == "short_clean":
        eligible = (data["ctx_high_since_entry"] >= 0.10) & ((-data["ctx_drawdown_from_entry_high"]) >= 0.04)
        target = (data["future_drop12"] >= 0.06) & (data["short_adverse_before5"] <= 0.04)
    else:
        raise ValueError(task)
    data = data[eligible].copy()
    data["target"] = target.loc[data.index].astype(int)
    return data


def expert_metrics(rows: pd.DataFrame, score: np.ndarray) -> dict[str, Any]:
    if rows.empty:
        return {"rows": 0}
    y = rows["target"].astype(int).to_numpy()
    out: dict[str, Any] = {
        "rows": int(len(rows)),
        "events": int(rows[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "base_rate": float(y.mean()),
        "auc": maybe_auc(y, score),
        "ap": maybe_ap(y, score),
    }
    for q in (0.80, 0.90, 0.95):
        out[f"q{int(q * 100)}"] = expert_threshold_metrics(rows, y, score, q)
    return out


def expert_threshold_metrics(rows: pd.DataFrame, y: np.ndarray, score: np.ndarray, q: float) -> dict[str, Any]:
    if len(rows) == 0:
        return {"selected": 0, "precision": None, "coverage": None}
    threshold = float(np.quantile(score, q))
    selected = score >= threshold
    grp = rows.iloc[np.flatnonzero(selected)]
    all_events = rows[["symbol", "entry_time"]].drop_duplicates().shape[0]
    selected_events = grp[["symbol", "entry_time"]].drop_duplicates().shape[0]
    return {
        "threshold": round(threshold, 6),
        "selected": int(selected.sum()),
        "precision": float(y[selected].mean()) if int(selected.sum()) else None,
        "event_coverage": float(selected_events / all_events) if all_events else None,
        "selected_events": int(selected_events),
        "events": int(all_events),
        "median_future_up24": safe_median(grp, "future_up24"),
        "median_future_drop12": safe_median(grp, "future_drop12"),
        "median_future_drop72": safe_median(grp, "future_drop72"),
        "median_short_adverse_before5": safe_median(grp, "short_adverse_before5"),
        "behavior_mix": {str(k): int(v) for k, v in grp["behavior_state"].value_counts().to_dict().items()},
    }


def state_feature_columns(states: pd.DataFrame) -> list[str]:
    cols: list[str] = []
    for col in states.columns:
        if col in META_COLS:
            continue
        if pd.api.types.is_numeric_dtype(states[col]):
            cols.append(col)
    # Explicitly remove known future/label columns if new columns are added later.
    blocked = {c for c in cols if c.startswith("future_") or c.startswith("y_") or "adverse" in c}
    return [c for c in cols if c not in blocked]


def time_split(ts: np.ndarray) -> dict[str, np.ndarray]:
    unique = np.sort(np.unique(ts))
    q70, q85 = np.quantile(unique, [0.70, 0.85])
    embargo = 3 * DAY_MS
    return {
        "train": np.flatnonzero(ts <= q70),
        "val": np.flatnonzero((ts >= q70 + embargo) & (ts <= q85)),
        "test": np.flatnonzero(ts >= q85 + embargo),
    }


def split_summary(data: pd.DataFrame, split: dict[str, np.ndarray]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, idx in split.items():
        part = data.iloc[idx]
        out[name] = {
            "rows": int(len(part)),
            "events": int(part[["symbol", "entry_time"]].drop_duplicates().shape[0]) if len(part) else 0,
            "positives": int(part["target"].sum()) if len(part) else 0,
            "positive_rate": float(part["target"].mean()) if len(part) else None,
        }
    return out


def threshold_payload(y: np.ndarray, score: np.ndarray, q: float) -> dict[str, Any]:
    if len(score) == 0:
        return {"selected": 0, "precision": None, "recall": None}
    threshold = float(np.quantile(score, q))
    selected = score >= threshold
    positives = int((y == 1).sum())
    hits = int(((y == 1) & selected).sum())
    return {
        "threshold": round(threshold, 6),
        "selected": int(selected.sum()),
        "precision": float(hits / max(int(selected.sum()), 1)),
        "recall": float(hits / positives) if positives else None,
    }


def safe_log_loss(y: np.ndarray, prob: np.ndarray) -> float | None:
    if len(y) == 0:
        return None
    try:
        return float(log_loss(y, prob, labels=list(range(len(ROUTER_ORDER)))))
    except Exception:
        return None


def feature_importance(model: lgb.LGBMClassifier, cols: list[str], n: int = 25) -> list[dict[str, Any]]:
    imp = model.booster_.feature_importance(importance_type="gain")
    order = np.argsort(imp)[::-1][:n]
    return [{"feature": cols[int(i)], "gain": float(imp[int(i)])} for i in order]


def entry_profile(entries: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(entries)),
        "family_counts": {str(k): int(v) for k, v in entries["family"].fillna("none").value_counts().to_dict().items()},
        "target_mode_rate": float(entries["family"].isin(TARGET_MODES).mean()),
        "quality_target_mode_rate": float((entries["family"].isin(TARGET_MODES) & (entries["y_long_start"].astype(int) == 1)).mean()),
        "median_future_high_48h": safe_median(entries, "future_high_48h"),
        "median_adverse_before_up5": safe_median(entries, "adverse_before_up5"),
    }


def state_profile(states: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {
        "rows": int(len(states)),
        "family_counts": {str(k): int(v) for k, v in states["family"].fillna("none").value_counts().to_dict().items()},
        "behavior_counts": {str(k): int(v) for k, v in states["behavior_state"].value_counts().to_dict().items()},
    }
    return out


def mode_state_profile(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {"rows": 0}
    return {
        "rows": int(len(rows)),
        "events": int(rows[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "behavior_counts": {str(k): int(v) for k, v in rows["behavior_state"].value_counts().to_dict().items()},
        "top_exit_candidate_rows": int(((rows["ctx_high_since_entry"] >= 0.08) & (rows["ctx_ret_since_entry"] >= 0.03)).sum()),
        "short_candidate_rows": int(((rows["ctx_high_since_entry"] >= 0.10) & ((-rows["ctx_drawdown_from_entry_high"]) >= 0.04)).sum()),
        "median_high_since_entry": safe_median(rows, "ctx_high_since_entry"),
        "median_drawdown_from_high": safe_median_series(-rows["ctx_drawdown_from_entry_high"]),
    }


def safe_median(df: pd.DataFrame, col: str) -> float | None:
    if df.empty or col not in df:
        return None
    value = float(pd.to_numeric(df[col], errors="coerce").median())
    return value if np.isfinite(value) else None


def safe_median_series(series: pd.Series) -> float | None:
    value = float(pd.to_numeric(series, errors="coerce").median())
    return value if np.isfinite(value) else None


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Mode-Aware Lifecycle Experiment",
        "",
        "This report tests the redesigned framework: target lifecycle entry, dynamic mode routing, and per-mode top/short experts.",
        "",
        f"- Target modes: {', '.join(results['target_modes'])}",
        f"- Sequence model: `{results['model']}`",
        f"- Expert feature count: {results.get('expert_feature_count')}",
        "",
        "## Entry Models",
        "",
        "| Target | Base | AUC | AP | q90 Precision | q95 Precision | q95 Median Up48 | q95 Median Adverse Before +5% | q95 Family Mix |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, item in results["entry_models"].items():
        s = item["summary"]
        q95 = s.get("test_top_buckets", {}).get("q95", {})
        lines.append(
            f"| {name} | {pct(s.get('test_base_rate'))} | {num(s.get('test_auc'))} | {num(s.get('test_ap'))} | "
            f"{pct(s.get('test_q90', {}).get('precision'))} | {pct(s.get('test_q95', {}).get('precision'))} | "
            f"{pct(q95.get('median_future_high_48h'))} | {pct(q95.get('median_adverse_before_up5'))} | "
            f"{json.dumps(q95.get('family_mix', {}), ensure_ascii=False)} |"
        )

    router = results.get("router", {}).get("summary", {})
    lines += [
        "",
        "## Dynamic Router",
        "",
        f"- Test accuracy: {pct(router.get('test_accuracy'))}",
        f"- Test balanced accuracy: {pct(router.get('test_balanced_accuracy'))}",
        "",
        "| Mode | Base | AUC | AP | q90 Precision | q95 Precision |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode in ROUTER_ORDER:
        s = router.get("test_modes", {}).get(mode, {})
        lines.append(
            f"| {mode} | {pct(s.get('base_rate'))} | {num(s.get('auc'))} | {num(s.get('ap'))} | "
            f"{pct(s.get('q90', {}).get('precision'))} | {pct(s.get('q95', {}).get('precision'))} |"
        )

    lines += [
        "",
        "## Per-Mode Experts",
        "",
        "| Mode | Task | Rows | Base | AUC | q90 Precision | q95 Precision | q95 Coverage | q95 Up24 | q95 Drop12 | q95 Drop72 | q95 Short Adverse | q95 Behavior Mix |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for mode, mode_result in results["experts"].items():
        for task in ("top_exit", "short_clean"):
            s = mode_result.get(task, {})
            if s.get("skipped"):
                lines.append(f"| {mode} | {task} | skipped: {s['skipped']} | - | - | - | - | - | - | - | - | - | - |")
                continue
            test = s.get("test", {})
            q90 = test.get("q90", {})
            q95 = test.get("q95", {})
            lines.append(
                f"| {mode} | {task} | {test.get('rows', 0)} | {pct(test.get('base_rate'))} | {num(test.get('auc'))} | "
                f"{pct(q90.get('precision'))} | {pct(q95.get('precision'))} | {pct(q95.get('event_coverage'))} | "
                f"{pct(q95.get('median_future_up24'))} | {pct(q95.get('median_future_drop12'))} | "
                f"{pct(q95.get('median_future_drop72'))} | {pct(q95.get('median_short_adverse_before5'))} | "
                f"{json.dumps(q95.get('behavior_mix', {}), ensure_ascii=False)} |"
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
