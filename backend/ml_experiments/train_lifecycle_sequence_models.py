"""Train sequence models for lifecycle start and dynamic family classification.

This is the second-stage lifecycle experiment:

1. `long_pump_event`: use the pre-entry long sequence to detect a strong
   lifecycle start.
2. `family`: use the sequence before and after the long entry to dynamically
   classify the realized pump family.

The goal is to test whether TCN/GRU/Transformer can outperform the static LGB
snapshot baseline for dynamic classification. It writes experiment artifacts
only and does not modify production models.

Example:
    python -m ml_experiments.train_lifecycle_sequence_models --source "E:\\2C2G\\币安数据库" --models tcn_small,gru_medium
"""
from __future__ import annotations

import argparse
import json
import math
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    log_loss,
    precision_score,
    roc_auc_score,
)
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from ml_experiments.build_sequence_dataset import (
    add_state_features,
    aggregate_15m,
    make_features,
    read_klines,
)
from ml_experiments.train_event_family_classifier import FAMILY_ORDER, OPERATIONAL_TARGETS

BAR_MS = 900_000
DAY_MS = 86_400_000

ENTRY_STATIC_COLS = [
    "qv30_rank_pct",
    "ret30_rank_pct",
    "qv30_ratio",
    "body_break_8",
]

FAMILY_CONTEXT_NAMES = [
    "is_after_entry",
    "bars_since_entry_scaled",
    "ret_since_entry",
    "high_since_entry",
    "drawdown_from_entry_high",
]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str
    params: dict[str, Any]


MODEL_SPECS = [
    ModelSpec("tcn_small", "tcn", {"channels": 32, "levels": 3, "dropout": 0.15}),
    ModelSpec("tcn_deep", "tcn", {"channels": 48, "levels": 4, "dropout": 0.20}),
    ModelSpec("gru_medium", "gru", {"hidden": 96, "layers": 1, "dropout": 0.15}),
    ModelSpec("gru_stack", "gru", {"hidden": 64, "layers": 2, "dropout": 0.20}),
    ModelSpec("tiny_transformer", "transformer", {"d_model": 32, "heads": 2, "layers": 2, "dropout": 0.15}),
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args()
    torch.set_num_threads(args.num_threads)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = Path(args.dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    entry_npz = dataset_dir / "lifecycle_seq_entry.npz"
    family_npz = dataset_dir / "lifecycle_seq_family.npz"
    if args.rebuild or not entry_npz.exists() or not family_npz.exists():
        build_sequence_datasets(args, entry_npz, family_npz)

    selected = [s for s in MODEL_SPECS if not args.models or s.name in args.models.split(",")]
    if not selected:
        raise SystemExit("no model specs selected")

    results: dict[str, Any] = {
        "source": args.source,
        "seq_len": args.seq_len,
        "include_state": args.include_state,
        "entry_dataset": str(entry_npz),
        "family_dataset": str(family_npz),
        "models": {"long_pump_event": [], "family": []},
    }

    entry_data = load_npz(entry_npz)
    family_data = load_npz(family_npz)
    for spec in selected:
        print(f"training long_pump_event {spec.name}", flush=True)
        res = train_binary(spec, entry_data, "y_pump_event", out_dir / "long_pump_event", args)
        results["models"]["long_pump_event"].append(res)
        print(json.dumps(res["summary"], ensure_ascii=False), flush=True)

    for spec in selected:
        print(f"training family {spec.name}", flush=True)
        res = train_family(spec, family_data, out_dir / "family", args)
        results["models"]["family"].append(res)
        print(json.dumps(res["summary"], ensure_ascii=False), flush=True)

    results["models"]["long_pump_event"].sort(key=lambda r: r["summary"]["test_rank_score"], reverse=True)
    results["models"]["family"].sort(key=lambda r: r["summary"]["test_rank_score"], reverse=True)
    results["baseline_lgb"] = load_lgb_baseline(Path(args.lgb_baseline))

    out_json = out_dir / "lifecycle_sequence_results.json"
    out_md = out_dir / "lifecycle_sequence_results.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"report": str(out_md), "json": str(out_json)}, ensure_ascii=False), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train sequence lifecycle start/family models.")
    parser.add_argument("--source", default=r"E:\2C2G\币安数据库")
    parser.add_argument("--entries", default="storage/ml/lifecycle/long_entries.parquet")
    parser.add_argument("--states", default="storage/ml/lifecycle/state_rows.parquet")
    parser.add_argument("--dataset-dir", default="storage/ml/lifecycle_seq")
    parser.add_argument("--out-dir", default="storage/ml/lifecycle_seq_runs")
    parser.add_argument("--lgb-baseline", default="storage/ml/lifecycle_models.json")
    parser.add_argument("--models", default="tcn_small,gru_medium,tiny_transformer")
    parser.add_argument("--seq-len", type=int, default=192, help="192 15m bars = 48h")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--include-state", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--epochs", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--min-family-samples", type=int, default=0)
    return parser.parse_args()


def build_sequence_datasets(args: argparse.Namespace, entry_npz: Path, family_npz: Path) -> None:
    source = Path(args.source)
    entries = pd.read_parquet(args.entries).copy()
    states = pd.read_parquet(args.states).copy()
    states = states[states["family"].isin(FAMILY_ORDER)].copy()
    if args.max_symbols > 0:
        symbols = sorted(entries.symbol.unique())[: args.max_symbols]
        entries = entries[entries.symbol.isin(symbols)].copy()
        states = states[states.symbol.isin(symbols)].copy()
    print(f"building sequence datasets symbols={entries.symbol.nunique()} entries={len(entries)} states={len(states)}", flush=True)

    entry_parts: list[dict[str, Any]] = []
    family_parts: list[dict[str, Any]] = []
    skipped: list[str] = []
    feature_names_entry: list[str] | None = None
    feature_names_family: list[str] | None = None
    symbol_names: list[str] = []
    symbol_to_id: dict[str, int] = {}

    for i, symbol in enumerate(sorted(entries.symbol.unique()), 1):
        path = source / "klines" / f"{symbol}.parquet"
        if not path.exists():
            skipped.append(f"{symbol}:missing_klines")
            continue
        try:
            raw = read_klines(path, args.days)
            bars = aggregate_15m(raw, min_minutes=10)
            if args.include_state:
                bars = add_state_features(source, symbol, bars)
            base_features, base_names = make_features(bars)
        except Exception as exc:
            skipped.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:120]}")
            continue
        if len(bars) < args.seq_len + 10:
            skipped.append(f"{symbol}:too_few_bars")
            continue

        symbol_to_id.setdefault(symbol, len(symbol_names))
        if symbol_to_id[symbol] == len(symbol_names):
            symbol_names.append(symbol)
        sid = symbol_to_id[symbol]
        bar_times = bars["timestamp"].astype("int64").to_numpy()
        close = bars["close"].to_numpy(dtype=np.float64)
        high = bars["high"].to_numpy(dtype=np.float64)
        time_to_ix = {int(t): int(ix) for ix, t in enumerate(bar_times)}

        erows = entries[entries.symbol == symbol]
        e_part = build_entry_sequences(erows, base_features, base_names, bar_times, time_to_ix, sid, args.seq_len)
        if e_part:
            feature_names_entry = e_part["feature_names"]
            entry_parts.append(e_part)

        frows = states[states.symbol == symbol]
        f_part = build_family_sequences(frows, base_features, base_names, bars, close, high, bar_times, time_to_ix, sid, args.seq_len)
        if f_part:
            feature_names_family = f_part["feature_names"]
            family_parts.append(f_part)

        if i % 25 == 0:
            print(
                f"seq loaded {i}/{entries.symbol.nunique()} entry={sum(len(p['timestamp']) for p in entry_parts)} "
                f"family={sum(len(p['timestamp']) for p in family_parts)}",
                flush=True,
            )

    if not entry_parts:
        raise SystemExit("no entry sequences built")
    if not family_parts:
        raise SystemExit("no family sequences built")

    save_entry_npz(entry_npz, entry_parts, feature_names_entry or [], symbol_names, skipped, args)
    save_family_npz(family_npz, family_parts, feature_names_family or [], symbol_names, skipped, args)


def build_entry_sequences(
    rows: pd.DataFrame,
    base_features: np.ndarray,
    base_names: list[str],
    bar_times: np.ndarray,
    time_to_ix: dict[int, int],
    symbol_id: int,
    seq_len: int,
) -> dict[str, Any] | None:
    x_list: list[np.ndarray] = []
    y_pump: list[int] = []
    y_quality: list[int] = []
    timestamps: list[int] = []
    row_ids: list[str] = []
    family: list[str] = []
    for row in rows.itertuples(index=False):
        ix = time_to_ix.get(int(row.entry_time))
        if ix is None or ix < seq_len - 1:
            continue
        seq = base_features[ix - seq_len + 1 : ix + 1]
        if not np.isfinite(seq).all():
            continue
        static = np.array([safe_get(row, c, 0.0) for c in ENTRY_STATIC_COLS], dtype=np.float32)
        static_seq = np.repeat(static.reshape(1, -1), seq_len, axis=0)
        x_list.append(np.concatenate([seq, static_seq], axis=1).astype(np.float32))
        y_pump.append(int(row.y_pump_event))
        y_quality.append(int(row.y_long_start))
        timestamps.append(int(row.entry_time))
        row_ids.append(f"{row.symbol}-{int(row.entry_time)}")
        family.append(str(row.family) if isinstance(row.family, str) else "")
    if not x_list:
        return None
    return {
        "x": np.stack(x_list).astype(np.float32),
        "y_pump_event": np.array(y_pump, dtype=np.int8),
        "y_long_start": np.array(y_quality, dtype=np.int8),
        "timestamp": np.array(timestamps, dtype=np.int64),
        "symbol_id": np.full(len(x_list), symbol_id, dtype=np.int16),
        "row_id": np.array(row_ids, dtype=object),
        "family": np.array(family, dtype=object),
        "feature_names": base_names + ENTRY_STATIC_COLS,
    }


def build_family_sequences(
    rows: pd.DataFrame,
    base_features: np.ndarray,
    base_names: list[str],
    bars: pd.DataFrame,
    close: np.ndarray,
    high: np.ndarray,
    bar_times: np.ndarray,
    time_to_ix: dict[int, int],
    symbol_id: int,
    seq_len: int,
) -> dict[str, Any] | None:
    label_to_id = {v: i for i, v in enumerate(FAMILY_ORDER)}
    x_list: list[np.ndarray] = []
    y_family: list[int] = []
    timestamps: list[int] = []
    row_ids: list[str] = []
    stages: list[float] = []
    for row in rows.itertuples(index=False):
        ix = time_to_ix.get(int(row.decision_time))
        entry_ix = time_to_ix.get(int(row.entry_time))
        if ix is None or entry_ix is None or ix < seq_len - 1:
            continue
        seq_base = base_features[ix - seq_len + 1 : ix + 1]
        if not np.isfinite(seq_base).all():
            continue
        ctx = family_context_features(bar_times[ix - seq_len + 1 : ix + 1], close, high, ix - seq_len + 1, entry_ix, ix, seq_len)
        seq = np.concatenate([seq_base, ctx], axis=1).astype(np.float32)
        if not np.isfinite(seq).all():
            continue
        x_list.append(seq)
        y_family.append(label_to_id[str(row.family)])
        timestamps.append(int(row.decision_time))
        row_ids.append(f"{row.symbol}-{int(row.entry_time)}-{int(row.decision_time)}")
        stages.append(float(row.stage_hours))
    if not x_list:
        return None
    return {
        "x": np.stack(x_list).astype(np.float32),
        "y_family": np.array(y_family, dtype=np.int64),
        "timestamp": np.array(timestamps, dtype=np.int64),
        "symbol_id": np.full(len(x_list), symbol_id, dtype=np.int16),
        "row_id": np.array(row_ids, dtype=object),
        "stage_hours": np.array(stages, dtype=np.float32),
        "feature_names": base_names + FAMILY_CONTEXT_NAMES,
    }


def family_context_features(
    seq_times: np.ndarray,
    close: np.ndarray,
    high: np.ndarray,
    seq_start_ix: int,
    entry_ix: int,
    decision_ix: int,
    seq_len: int,
) -> np.ndarray:
    entry_close = float(close[entry_ix])
    out = np.zeros((len(seq_times), len(FAMILY_CONTEXT_NAMES)), dtype=np.float32)
    for local_ix in range(len(seq_times)):
        ix = seq_start_ix + local_ix
        after = ix >= entry_ix
        if after:
            hi_since = float(np.max(high[entry_ix : ix + 1]))
            out[local_ix, 0] = 1.0
            out[local_ix, 1] = float(np.clip((ix - entry_ix) / max(seq_len, 1), -1.0, 2.0))
            out[local_ix, 2] = float(close[ix] / entry_close - 1.0)
            out[local_ix, 3] = float(hi_since / entry_close - 1.0)
            out[local_ix, 4] = float(close[ix] / hi_since - 1.0)
        else:
            out[local_ix, 0] = 0.0
            out[local_ix, 1] = float(np.clip((ix - entry_ix) / max(seq_len, 1), -1.0, 0.0))
            out[local_ix, 2] = float(close[ix] / entry_close - 1.0)
    return out


def save_entry_npz(path: Path, parts: list[dict[str, Any]], feature_names: list[str], symbol_names: list[str], skipped: list[str], args: argparse.Namespace) -> None:
    x = np.concatenate([p["x"] for p in parts], axis=0)
    y_pump = np.concatenate([p["y_pump_event"] for p in parts])
    y_quality = np.concatenate([p["y_long_start"] for p in parts])
    ts = np.concatenate([p["timestamp"] for p in parts])
    sid = np.concatenate([p["symbol_id"] for p in parts])
    row_id = np.concatenate([p["row_id"] for p in parts])
    family = np.concatenate([p["family"] for p in parts])
    order = np.argsort(ts, kind="mergesort")
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        x=x[order],
        y_pump_event=y_pump[order],
        y_long_start=y_quality[order],
        timestamp=ts[order],
        symbol_id=sid[order],
        row_id=row_id[order],
        family=family[order],
        feature_names=np.array(feature_names, dtype=object),
        symbol_names=np.array(symbol_names, dtype=object),
    )
    write_npz_meta(path, x[order], {"y_pump_event": y_pump[order], "y_long_start": y_quality[order]}, feature_names, symbol_names, skipped, args)


def save_family_npz(path: Path, parts: list[dict[str, Any]], feature_names: list[str], symbol_names: list[str], skipped: list[str], args: argparse.Namespace) -> None:
    x = np.concatenate([p["x"] for p in parts], axis=0)
    y = np.concatenate([p["y_family"] for p in parts])
    ts = np.concatenate([p["timestamp"] for p in parts])
    sid = np.concatenate([p["symbol_id"] for p in parts])
    row_id = np.concatenate([p["row_id"] for p in parts])
    stage = np.concatenate([p["stage_hours"] for p in parts])
    order = np.argsort(ts, kind="mergesort")
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        x=x[order],
        y_family=y[order],
        timestamp=ts[order],
        symbol_id=sid[order],
        row_id=row_id[order],
        stage_hours=stage[order],
        family_order=np.array(FAMILY_ORDER, dtype=object),
        feature_names=np.array(feature_names, dtype=object),
        symbol_names=np.array(symbol_names, dtype=object),
    )
    write_npz_meta(path, x[order], {"y_family": y[order]}, feature_names, symbol_names, skipped, args)


def write_npz_meta(path: Path, x: np.ndarray, labels: dict[str, np.ndarray], feature_names: list[str], symbol_names: list[str], skipped: list[str], args: argparse.Namespace) -> None:
    meta = {
        "path": str(path),
        "samples": int(len(x)),
        "seq_len": int(x.shape[1]),
        "features": int(x.shape[2]),
        "feature_names": feature_names,
        "symbols": len(symbol_names),
        "label_rates": {k: label_summary(v) for k, v in labels.items()},
        "include_state": bool(args.include_state),
        "skipped_count": len(skipped),
        "skipped": skipped[:200],
    }
    path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False), flush=True)


def label_summary(values: np.ndarray) -> Any:
    if values.ndim == 1 and values.dtype.kind in {"i", "u"} and values.max(initial=0) > 1:
        return {FAMILY_ORDER[int(k)]: int(v) for k, v in zip(*np.unique(values, return_counts=True))}
    return float(np.mean(values)) if len(values) else None


def load_npz(path: Path) -> dict[str, Any]:
    data = np.load(path, allow_pickle=True)
    return {k: data[k] for k in data.files}


def train_binary(spec: ModelSpec, data: dict[str, Any], target: str, out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    seed_everything(args.seed, spec.name + target)
    out_dir.mkdir(parents=True, exist_ok=True)
    x = data["x"].astype(np.float32)
    y = data[target].astype(np.float32)
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
            "state_dict": model.state_dict(),
        },
        model_path,
    )
    summary = {
        "task": target,
        "model": spec.name,
        "kind": spec.kind,
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


def train_family(spec: ModelSpec, data: dict[str, Any], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    seed_everything(args.seed, spec.name + "family")
    out_dir.mkdir(parents=True, exist_ok=True)
    x = data["x"].astype(np.float32)
    y = data["y_family"].astype(np.int64)
    ts = data["timestamp"].astype(np.int64)
    split = chronological_split(ts)
    scaler = fit_scaler(x[split["train"]])
    x_train = apply_scaler(x[split["train"]], scaler)
    x_val = apply_scaler(x[split["val"]], scaler)
    x_test = apply_scaler(x[split["test"]], scaler)
    y_train, y_val, y_test = y[split["train"]], y[split["val"]], y[split["test"]]
    model = build_model(spec, x.shape[-1], x.shape[1], output_dim=len(FAMILY_ORDER))
    counts = np.bincount(y_train, minlength=len(FAMILY_ORDER)).astype(np.float32)
    weights = counts.sum() / np.maximum(counts, 1.0)
    weights = weights / weights.mean()
    loss_fn = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32))
    model, history = fit_torch_model(model, x_train, y_train, x_val, y_val, loss_fn, args, multiclass=True)
    val_prob = predict_multiclass(model, x_val)
    test_prob = predict_multiclass(model, x_test)
    val = family_metrics(y_val, val_prob)
    test = family_metrics(y_test, test_prob)
    run_dir = out_dir / f"family_{spec.name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "model.pt"
    torch.save(
        {
            "task": "family",
            "family_order": FAMILY_ORDER,
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
        "task": "family",
        "model": spec.name,
        "kind": spec.kind,
        "samples": int(len(x)),
        "train_samples": int(len(y_train)),
        "val_samples": int(len(y_val)),
        "test_samples": int(len(y_test)),
        "class_counts": {FAMILY_ORDER[int(k)]: int(v) for k, v in zip(*np.unique(y, return_counts=True))},
        **{f"val_{k}": v for k, v in val.items()},
        **{f"test_{k}": v for k, v in test.items()},
        "model_path": str(model_path),
    }
    summary["val_rank_score"] = family_rank_score(summary, "val")
    summary["test_rank_score"] = family_rank_score(summary, "test")
    (run_dir / "metrics.json").write_text(json.dumps({"summary": summary, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary": summary, "history": history}


def fit_torch_model(
    model: nn.Module,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    loss_fn: nn.Module,
    args: argparse.Namespace,
    multiclass: bool,
) -> tuple[nn.Module, list[dict[str, Any]]]:
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    y_train_tensor = torch.from_numpy(y_train.astype(np.int64 if multiclass else np.float32))
    y_val_tensor = torch.from_numpy(y_val.astype(np.int64 if multiclass else np.float32))
    train_loader = DataLoader(TensorDataset(torch.from_numpy(x_train), y_train_tensor), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(TensorDataset(torch.from_numpy(x_val), y_val_tensor), batch_size=args.batch_size)
    best_score = -math.inf
    best_state = None
    stale = 0
    history: list[dict[str, Any]] = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        for xb, yb in train_loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(xb)
            loss = loss_fn(logits, yb) if multiclass else loss_fn(logits.squeeze(-1), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
            total_loss += float(loss.detach()) * len(xb)
        if multiclass:
            val_prob = predict_multiclass_loader(model, val_loader)
            metrics = family_metrics(y_val, val_prob)
            score = family_rank_score({f"val_{k}": v for k, v in metrics.items()}, "val")
        else:
            val_pred = predict_binary_loader(model, val_loader)
            metrics = binary_metrics(y_val, val_pred)
            score = binary_rank_score({f"val_{k}": v for k, v in metrics.items()}, "val")
        history.append({"epoch": epoch, "train_loss": total_loss / max(len(x_train), 1), **{f"val_{k}": v for k, v in metrics.items()}})
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


def build_model(spec: ModelSpec, features: int, seq_len: int, output_dim: int) -> nn.Module:
    if spec.kind == "tcn":
        return TCN(features, output_dim=output_dim, **spec.params)
    if spec.kind == "gru":
        return GRUClassifier(features, output_dim=output_dim, **spec.params)
    if spec.kind == "transformer":
        return TinyTransformer(features, seq_len=seq_len, output_dim=output_dim, **spec.params)
    raise ValueError(f"unknown model kind {spec.kind}")


class TCNBlock(nn.Module):
    def __init__(self, channels: int, dilation: int, dropout: float):
        super().__init__()
        padding = dilation * 2
        self.net = nn.Sequential(
            nn.Conv1d(channels, channels, kernel_size=3, padding=padding, dilation=dilation),
            Chomp1d(padding),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Conv1d(channels, channels, kernel_size=3, padding=padding, dilation=dilation),
            Chomp1d(padding),
            nn.SiLU(),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.net(x)


class Chomp1d(nn.Module):
    def __init__(self, chomp: int):
        super().__init__()
        self.chomp = chomp

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x[:, :, : -self.chomp] if self.chomp else x


class TCN(nn.Module):
    def __init__(self, features: int, output_dim: int, channels: int = 32, levels: int = 3, dropout: float = 0.15):
        super().__init__()
        blocks: list[nn.Module] = [nn.Conv1d(features, channels, kernel_size=1)]
        for i in range(levels):
            blocks.append(TCNBlock(channels, dilation=2**i, dropout=dropout))
        self.net = nn.Sequential(*blocks)
        self.head = nn.Sequential(nn.AdaptiveAvgPool1d(1), nn.Flatten(), nn.Linear(channels, output_dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.net(x.transpose(1, 2)))


class GRUClassifier(nn.Module):
    def __init__(self, features: int, output_dim: int, hidden: int = 96, layers: int = 1, dropout: float = 0.15):
        super().__init__()
        self.gru = nn.GRU(features, hidden, num_layers=layers, batch_first=True, dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.LayerNorm(hidden), nn.Dropout(dropout), nn.Linear(hidden, output_dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, h = self.gru(x)
        return self.head(h[-1])


class TinyTransformer(nn.Module):
    def __init__(
        self,
        features: int,
        seq_len: int,
        output_dim: int,
        d_model: int = 32,
        heads: int = 2,
        layers: int = 2,
        dropout: float = 0.15,
    ):
        super().__init__()
        self.proj = nn.Linear(features, d_model)
        self.pos = nn.Parameter(torch.zeros(1, seq_len, d_model))
        layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=heads, dim_feedforward=d_model * 4, dropout=dropout, batch_first=True, activation="gelu")
        self.encoder = nn.TransformerEncoder(layer, num_layers=layers)
        self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Dropout(dropout), nn.Linear(d_model, output_dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.proj(x) + self.pos[:, : x.shape[1]]
        z = self.encoder(z)
        return self.head(z[:, -1])


def chronological_split(ts: np.ndarray) -> dict[str, np.ndarray]:
    unique = np.sort(np.unique(ts))
    q70, q85 = np.quantile(unique, [0.70, 0.85])
    embargo = 3 * DAY_MS
    train = np.flatnonzero(ts <= q70)
    val = np.flatnonzero((ts >= q70 + embargo) & (ts <= q85))
    test = np.flatnonzero(ts >= q85 + embargo)
    return {"train": train, "val": val, "test": test}


def fit_scaler(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    flat = x.reshape(-1, x.shape[-1])
    mean = np.nanmean(flat, axis=0).astype(np.float32)
    std = np.nanstd(flat, axis=0).astype(np.float32)
    mean[~np.isfinite(mean)] = 0.0
    std[~np.isfinite(std) | (std < 1e-6)] = 1.0
    return mean, std


def apply_scaler(x: np.ndarray, scaler: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    mean, std = scaler
    out = (x - mean.reshape(1, 1, -1)) / std.reshape(1, 1, -1)
    out[~np.isfinite(out)] = 0.0
    return out.astype(np.float32)


@torch.no_grad()
def predict_binary(model: nn.Module, x: np.ndarray) -> np.ndarray:
    loader = DataLoader(TensorDataset(torch.from_numpy(x), torch.zeros(len(x))), batch_size=512)
    return predict_binary_loader(model, loader)


@torch.no_grad()
def predict_binary_loader(model: nn.Module, loader: DataLoader) -> np.ndarray:
    model.eval()
    preds: list[np.ndarray] = []
    for xb, _ in loader:
        preds.append(torch.sigmoid(model(xb).squeeze(-1)).cpu().numpy())
    return np.concatenate(preds) if preds else np.array([], dtype=np.float32)


@torch.no_grad()
def predict_multiclass(model: nn.Module, x: np.ndarray) -> np.ndarray:
    loader = DataLoader(TensorDataset(torch.from_numpy(x), torch.zeros(len(x), dtype=torch.long)), batch_size=512)
    return predict_multiclass_loader(model, loader)


@torch.no_grad()
def predict_multiclass_loader(model: nn.Module, loader: DataLoader) -> np.ndarray:
    model.eval()
    preds: list[np.ndarray] = []
    for xb, _ in loader:
        preds.append(torch.softmax(model(xb), dim=1).cpu().numpy())
    return np.concatenate(preds) if preds else np.empty((0, len(FAMILY_ORDER)), dtype=np.float32)


def binary_metrics(y: np.ndarray, pred: np.ndarray) -> dict[str, Any]:
    out = {
        "base_rate": float(y.mean()) if len(y) else None,
        "auc": maybe_auc(y, pred),
        "ap": maybe_ap(y, pred),
        "p_at_20": precision_at_k(y, pred, 20),
        "p_at_50": precision_at_k(y, pred, 50),
        "p_at_100": precision_at_k(y, pred, 100),
        "p_top_5pct": precision_at_fraction(y, pred, 0.05),
        "threshold_050_precision": float(precision_score(y, pred >= 0.5, zero_division=0)) if len(y) else None,
    }
    for q in (0.80, 0.90, 0.95):
        out[f"q{int(q * 100)}"] = threshold_metrics(y, pred, q)
    return out


def family_metrics(y: np.ndarray, prob: np.ndarray) -> dict[str, Any]:
    pred = np.argmax(prob, axis=1) if len(prob) else np.array([], dtype=np.int64)
    out: dict[str, Any] = {
        "accuracy": float(accuracy_score(y, pred)) if len(y) else None,
        "balanced_accuracy": float(balanced_accuracy_score(y, pred)) if len(np.unique(y)) > 1 else None,
        "log_loss": safe_log_loss(y, prob),
    }
    label_to_id = {v: i for i, v in enumerate(FAMILY_ORDER)}
    for target, families in OPERATIONAL_TARGETS.items():
        ids = [label_to_id[f] for f in families]
        score = prob[:, ids].sum(axis=1) if len(prob) else np.array([], dtype=np.float32)
        yy = np.isin(y, ids).astype(np.int8)
        out[target] = {
            "base_rate": float(yy.mean()) if len(yy) else None,
            "auc": maybe_auc(yy, score),
            "ap": maybe_ap(yy, score),
            "q90": threshold_metrics(yy, score, 0.90),
            "q95": threshold_metrics(yy, score, 0.95),
        }
    return out


def safe_log_loss(y: np.ndarray, prob: np.ndarray) -> float | None:
    if len(y) == 0:
        return None
    try:
        return float(log_loss(y, prob, labels=list(range(len(FAMILY_ORDER)))))
    except Exception:
        return None


def threshold_metrics(y: np.ndarray, score: np.ndarray, quantile: float) -> dict[str, Any]:
    if len(score) == 0:
        return {"selected": 0, "precision": None, "recall": None, "threshold": None}
    threshold = float(np.quantile(score, quantile))
    selected = score >= threshold
    hits = int((selected & (y == 1)).sum())
    total = int(selected.sum())
    positives = int((y == 1).sum())
    return {
        "threshold": round(threshold, 6),
        "selected": total,
        "precision": round(hits / total, 6) if total else None,
        "recall": round(hits / positives, 6) if positives else None,
    }


def maybe_auc(y: np.ndarray, pred: np.ndarray) -> float | None:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, pred))


def maybe_ap(y: np.ndarray, pred: np.ndarray) -> float | None:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    return float(average_precision_score(y, pred))


def precision_at_k(y: np.ndarray, pred: np.ndarray, k: int) -> float | None:
    if len(y) == 0:
        return None
    k = min(k, len(y))
    idx = np.argsort(pred)[-k:]
    return float(y[idx].mean())


def precision_at_fraction(y: np.ndarray, pred: np.ndarray, frac: float) -> float | None:
    return precision_at_k(y, pred, max(1, int(len(y) * frac)))


def binary_rank_score(summary: dict[str, Any], prefix: str) -> float:
    return float(
        0.35 * nz(summary.get(f"{prefix}_auc"))
        + 0.25 * nz(summary.get(f"{prefix}_ap"))
        + 0.20 * nz(summary.get(f"{prefix}_q90", {}).get("precision"))
        + 0.20 * nz(summary.get(f"{prefix}_p_top_5pct"))
    )


def family_rank_score(summary: dict[str, Any], prefix: str) -> float:
    fast = summary.get(f"{prefix}_fast_dump", {})
    slow = summary.get(f"{prefix}_slow_or_second_distribution", {})
    return float(
        0.30 * nz(fast.get("auc"))
        + 0.25 * nz(fast.get("q90", {}).get("precision"))
        + 0.20 * nz(fast.get("q95", {}).get("precision"))
        + 0.15 * nz(slow.get("auc"))
        + 0.10 * nz(summary.get(f"{prefix}_balanced_accuracy"))
    )


def nz(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return 0.0
    return out if np.isfinite(out) else 0.0


def seed_everything(seed: int, salt: str) -> None:
    s = int(seed) + zlib.adler32(salt.encode("utf-8")) % 10_000
    np.random.seed(s)
    torch.manual_seed(s)


def safe_get(row: Any, name: str, default: float) -> float:
    try:
        value = getattr(row, name)
    except Exception:
        return default
    try:
        out = float(value)
    except Exception:
        return default
    return out if np.isfinite(out) else default


def load_lgb_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    models = data.get("models", {})
    return {
        "long_pump_event": models.get("long_pump_event", {}).get("holdout"),
        "family": models.get("family", {}).get("holdout"),
    }


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Lifecycle Sequence Models",
        "",
        "Sequence experiment for lifecycle start and dynamic family classification.",
        "",
        f"- Source: `{results['source']}`",
        f"- Sequence length: {results['seq_len']} bars",
        f"- Include state: {results['include_state']}",
        "",
        "## Long Pump Event",
        "",
        "| Model | Test AUC | Test AP | q90 Precision | q95 Precision | Top5% Precision |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in results["models"]["long_pump_event"]:
        s = item["summary"]
        lines.append(
            f"| {s['model']} | {num(s.get('test_auc'))} | {num(s.get('test_ap'))} | "
            f"{pct(s.get('test_q90', {}).get('precision'))} | {pct(s.get('test_q95', {}).get('precision'))} | "
            f"{pct(s.get('test_p_top_5pct'))} |"
        )
    lines += [
        "",
        "## Dynamic Family",
        "",
        "| Model | Acc | Bal Acc | Fast AUC | Fast q90 | Fast q95 | Slow AUC | Slow q90 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results["models"]["family"]:
        s = item["summary"]
        fast = s.get("test_fast_dump", {})
        slow = s.get("test_slow_or_second_distribution", {})
        lines.append(
            f"| {s['model']} | {pct(s.get('test_accuracy'))} | {pct(s.get('test_balanced_accuracy'))} | "
            f"{num(fast.get('auc'))} | {pct(fast.get('q90', {}).get('precision'))} | "
            f"{pct(fast.get('q95', {}).get('precision'))} | {num(slow.get('auc'))} | "
            f"{pct(slow.get('q90', {}).get('precision'))} |"
        )
    baseline = results.get("baseline_lgb")
    if baseline:
        lines += [
            "",
            "## LGB Baseline Reference",
            "",
            f"- Long pump event baseline: AUC {num(baseline.get('long_pump_event', {}).get('auc'))}, q90 {pct(baseline.get('long_pump_event', {}).get('thresholds', {}).get('q90', {}).get('precision'))}",
            f"- Family fast_dump baseline: AUC {num(baseline.get('family', {}).get('operational', {}).get('fast_dump', {}).get('auc'))}, q90 {pct(baseline.get('family', {}).get('operational', {}).get('fast_dump', {}).get('thresholds', {}).get('q90', {}).get('precision'))}",
        ]
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
