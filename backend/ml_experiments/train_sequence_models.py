from __future__ import annotations

import argparse
import json
import math
import os
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sklearn.metrics import average_precision_score, precision_score, roc_auc_score
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str
    params: dict[str, Any]


MODEL_SPECS = [
    ModelSpec("cnn_small", "cnn", {"channels": 32, "dropout": 0.15}),
    ModelSpec("cnn_wide", "cnn", {"channels": 64, "dropout": 0.20}),
    ModelSpec("tcn_small", "tcn", {"channels": 32, "levels": 3, "dropout": 0.15}),
    ModelSpec("tcn_deep", "tcn", {"channels": 48, "levels": 4, "dropout": 0.20}),
    ModelSpec("tcn_wide", "tcn", {"channels": 64, "levels": 3, "dropout": 0.20}),
    ModelSpec("gru_small", "gru", {"hidden": 48, "layers": 1, "dropout": 0.10}),
    ModelSpec("gru_medium", "gru", {"hidden": 96, "layers": 1, "dropout": 0.15}),
    ModelSpec("gru_stack", "gru", {"hidden": 64, "layers": 2, "dropout": 0.20}),
    ModelSpec("tiny_transformer", "transformer", {"d_model": 32, "heads": 2, "layers": 2, "dropout": 0.15}),
    ModelSpec("transformer_wide", "transformer", {"d_model": 64, "heads": 4, "layers": 2, "dropout": 0.20}),
    ModelSpec("transformer_deep", "transformer", {"d_model": 48, "heads": 4, "layers": 3, "dropout": 0.20}),
]


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_meta = load_dataset(args.dataset, args.task)
    selected_specs = [s for s in MODEL_SPECS if not args.models or s.name in args.models.split(",")]
    if not selected_specs:
        raise SystemExit("no model specs selected")

    common = {
        "dataset": args.dataset,
        "task": args.task,
        "out_dir": str(out_dir),
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "lr": args.lr,
        "patience": args.patience,
        "seed": args.seed,
        "num_threads": args.num_threads,
    }
    jobs = min(args.jobs, len(selected_specs))
    print(f"training {len(selected_specs)} specs with jobs={jobs}", flush=True)
    results = []
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(train_one, spec, common) for spec in selected_specs]
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            print(json.dumps(result["summary"], ensure_ascii=False), flush=True)

    results.sort(key=lambda r: r["summary"].get("val_rank_score", 0.0), reverse=True)
    ranking = {
        "dataset": args.dataset,
        "task": args.task,
        "samples": int(len(data_meta["y"])),
        "feature_count": int(data_meta["x"].shape[-1]),
        "seq_len": int(data_meta["x"].shape[1]),
        "ranking": [r["summary"] for r in results],
    }
    (out_dir / f"ranking_{args.task}.json").write_text(json.dumps(ranking, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(ranking, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train sequence models for top/dump prediction.")
    parser.add_argument("--dataset", default="storage/ml/sequence_dataset.npz")
    parser.add_argument("--task", choices=["top", "dump"], default="dump")
    parser.add_argument("--out-dir", default="storage/ml/runs")
    parser.add_argument("--models", default="", help="Comma list, e.g. cnn_small,tcn_small,gru_small,tiny_transformer")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--patience", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-threads", type=int, default=2)
    return parser.parse_args()


def load_dataset(path: str | Path, task: str) -> dict[str, Any]:
    data = np.load(path, allow_pickle=True)
    y_key = "y_top" if task == "top" else "y_dump"
    return {
        "x": data["x"].astype(np.float32),
        "y": data[y_key].astype(np.float32),
        "timestamp": data["timestamp"].astype(np.int64),
        "symbol_id": data["symbol_id"].astype(np.int16),
        "feature_names": [str(x) for x in data["feature_names"].tolist()],
        "symbol_names": [str(x) for x in data["symbol_names"].tolist()],
    }


def train_one(spec: ModelSpec, common: dict[str, Any]) -> dict[str, Any]:
    torch.set_num_threads(int(common["num_threads"]))
    seed = int(common["seed"]) + zlib.adler32(spec.name.encode("utf-8")) % 10_000
    np.random.seed(seed)
    torch.manual_seed(seed)

    task = common["task"]
    out_dir = Path(common["out_dir"])
    run_dir = out_dir / f"{task}_{spec.name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    dataset = load_dataset(common["dataset"], task)
    x, y, ts = dataset["x"], dataset["y"], dataset["timestamp"]
    split = chronological_split(ts)
    train_idx, val_idx, test_idx = split["train"], split["val"], split["test"]
    mean, std = fit_scaler(x[train_idx])
    x_train = apply_scaler(x[train_idx], mean, std)
    x_val = apply_scaler(x[val_idx], mean, std)
    x_test = apply_scaler(x[test_idx], mean, std)
    y_train, y_val, y_test = y[train_idx], y[val_idx], y[test_idx]

    train_loader = DataLoader(
        TensorDataset(torch.from_numpy(x_train), torch.from_numpy(y_train)),
        batch_size=int(common["batch_size"]),
        shuffle=True,
        drop_last=False,
    )
    val_loader = DataLoader(TensorDataset(torch.from_numpy(x_val), torch.from_numpy(y_val)), batch_size=int(common["batch_size"]))
    test_loader = DataLoader(TensorDataset(torch.from_numpy(x_test), torch.from_numpy(y_test)), batch_size=int(common["batch_size"]))

    model = build_model(spec, x.shape[-1], x.shape[1])
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(common["lr"]), weight_decay=1e-4)
    pos = float(y_train.sum())
    neg = float(len(y_train) - pos)
    pos_weight = torch.tensor([neg / max(pos, 1.0)], dtype=torch.float32)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    best_val = -math.inf
    best_state = None
    stale = 0
    history = []
    for epoch in range(1, int(common["epochs"]) + 1):
        model.train()
        train_loss = 0.0
        for xb, yb in train_loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(xb).squeeze(-1)
            loss = loss_fn(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
            train_loss += float(loss.detach()) * len(xb)
        val_pred = predict(model, val_loader)
        val_metrics = score_predictions(y_val, val_pred)
        val_score = val_metrics["auc"] + 0.25 * val_metrics["ap"]
        history.append({"epoch": epoch, "train_loss": train_loss / max(len(y_train), 1), **{f"val_{k}": v for k, v in val_metrics.items()}})
        if val_score > best_val:
            best_val = val_score
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
        if stale >= int(common["patience"]):
            break

    if best_state:
        model.load_state_dict(best_state)
    val_pred = predict(model, val_loader)
    test_pred = predict(model, test_loader)
    val_metrics = score_predictions(y_val, val_pred)
    test_metrics = score_predictions(y_test, test_pred)
    bundle = {
        "model_name": spec.name,
        "model_kind": spec.kind,
        "model_params": spec.params,
        "task": task,
        "seq_len": int(x.shape[1]),
        "feature_names": dataset["feature_names"],
        "mean": mean,
        "std": std,
        "state_dict": model.state_dict(),
    }
    model_path = run_dir / "model.pt"
    torch.save(bundle, model_path)
    metrics = {
        "model": spec.name,
        "kind": spec.kind,
        "task": task,
        "train_samples": int(len(train_idx)),
        "val_samples": int(len(val_idx)),
        "test_samples": int(len(test_idx)),
        "positive_rate_train": float(y_train.mean()),
        "positive_rate_val": float(y_val.mean()),
        "positive_rate_test": float(y_test.mean()),
        **{f"val_{k}": v for k, v in val_metrics.items()},
        **{f"test_{k}": v for k, v in test_metrics.items()},
        "model_path": str(model_path),
    }
    metrics["val_rank_score"] = rank_score(metrics, "val")
    metrics["test_rank_score"] = rank_score(metrics, "test")
    (run_dir / "metrics.json").write_text(json.dumps({"summary": metrics, "history": history}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary": metrics, "history": history}


def chronological_split(ts: np.ndarray) -> dict[str, np.ndarray]:
    q70, q85 = np.quantile(ts, [0.70, 0.85])
    train = np.flatnonzero(ts <= q70)
    val = np.flatnonzero((ts > q70) & (ts <= q85))
    test = np.flatnonzero(ts > q85)
    return {"train": train, "val": val, "test": test}


def fit_scaler(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    flat = x.reshape(-1, x.shape[-1])
    mean = np.nanmean(flat, axis=0).astype(np.float32)
    std = np.nanstd(flat, axis=0).astype(np.float32)
    std[std < 1e-6] = 1.0
    mean[~np.isfinite(mean)] = 0.0
    std[~np.isfinite(std)] = 1.0
    return mean, std


def apply_scaler(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    out = (x - mean.reshape(1, 1, -1)) / std.reshape(1, 1, -1)
    out[~np.isfinite(out)] = 0.0
    return out.astype(np.float32)


def build_model(spec: ModelSpec, features: int, seq_len: int) -> nn.Module:
    if spec.kind == "cnn":
        return CNN1D(features, **spec.params)
    if spec.kind == "tcn":
        return TCN(features, **spec.params)
    if spec.kind == "gru":
        return GRUClassifier(features, **spec.params)
    if spec.kind == "transformer":
        return TinyTransformer(features, seq_len=seq_len, **spec.params)
    raise ValueError(f"unknown model kind {spec.kind}")


class CNN1D(nn.Module):
    def __init__(self, features: int, channels: int = 32, dropout: float = 0.15):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(features, channels, kernel_size=3, padding=1),
            nn.BatchNorm1d(channels),
            nn.SiLU(),
            nn.Conv1d(channels, channels, kernel_size=5, padding=2),
            nn.BatchNorm1d(channels),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Conv1d(channels, channels, kernel_size=3, padding=1),
            nn.SiLU(),
        )
        self.head = nn.Sequential(nn.AdaptiveMaxPool1d(1), nn.Flatten(), nn.Linear(channels, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.transpose(1, 2)
        return self.head(self.net(x))


class TCNBlock(nn.Module):
    def __init__(self, channels: int, dilation: int, dropout: float):
        super().__init__()
        padding = dilation * 2
        self.net = nn.Sequential(
            nn.Conv1d(channels, channels, kernel_size=3, padding=padding, dilation=dilation),
            nn.Chomp1d(padding) if hasattr(nn, "Chomp1d") else Chomp1d(padding),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Conv1d(channels, channels, kernel_size=3, padding=padding, dilation=dilation),
            nn.Chomp1d(padding) if hasattr(nn, "Chomp1d") else Chomp1d(padding),
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
    def __init__(self, features: int, channels: int = 32, levels: int = 3, dropout: float = 0.15):
        super().__init__()
        blocks: list[nn.Module] = [nn.Conv1d(features, channels, kernel_size=1)]
        for i in range(levels):
            blocks.append(TCNBlock(channels, dilation=2**i, dropout=dropout))
        self.net = nn.Sequential(*blocks)
        self.head = nn.Sequential(nn.AdaptiveAvgPool1d(1), nn.Flatten(), nn.Linear(channels, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.net(x.transpose(1, 2)))


class GRUClassifier(nn.Module):
    def __init__(self, features: int, hidden: int = 48, layers: int = 1, dropout: float = 0.10):
        super().__init__()
        self.gru = nn.GRU(features, hidden, num_layers=layers, batch_first=True, dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.LayerNorm(hidden), nn.Dropout(dropout), nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, h = self.gru(x)
        return self.head(h[-1])


class TinyTransformer(nn.Module):
    def __init__(self, features: int, seq_len: int, d_model: int = 32, heads: int = 2, layers: int = 2, dropout: float = 0.15):
        super().__init__()
        self.proj = nn.Linear(features, d_model)
        self.pos = nn.Parameter(torch.zeros(1, seq_len, d_model))
        encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=heads, dim_feedforward=d_model * 4, dropout=dropout, batch_first=True, activation="gelu")
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=layers)
        self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Dropout(dropout), nn.Linear(d_model, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z = self.proj(x) + self.pos[:, : x.shape[1]]
        z = self.encoder(z)
        return self.head(z[:, -1])


@torch.no_grad()
def predict(model: nn.Module, loader: DataLoader) -> np.ndarray:
    model.eval()
    preds = []
    for xb, _yb in loader:
        logits = model(xb).squeeze(-1)
        preds.append(torch.sigmoid(logits).cpu().numpy())
    return np.concatenate(preds) if preds else np.array([], dtype=np.float32)


def score_predictions(y: np.ndarray, pred: np.ndarray) -> dict[str, float]:
    if len(np.unique(y)) < 2:
        auc = 0.5
        ap = float(y.mean()) if len(y) else 0.0
    else:
        auc = float(roc_auc_score(y, pred))
        ap = float(average_precision_score(y, pred))
    out = {
        "auc": auc,
        "ap": ap,
        "p_at_20": precision_at_k(y, pred, 20),
        "p_at_50": precision_at_k(y, pred, 50),
        "p_at_100": precision_at_k(y, pred, 100),
        "p_at_500": precision_at_k(y, pred, 500),
        "p_top_1pct": precision_at_fraction(y, pred, 0.01),
        "p_top_5pct": precision_at_fraction(y, pred, 0.05),
        "threshold_050_precision": float(precision_score(y, pred >= 0.5, zero_division=0)),
    }
    return out


def rank_score(metrics: dict[str, float], prefix: str) -> float:
    return float(
        0.30 * metrics.get(f"{prefix}_auc", 0.0)
        + 0.25 * metrics.get(f"{prefix}_ap", 0.0)
        + 0.20 * metrics.get(f"{prefix}_p_at_50", 0.0)
        + 0.15 * metrics.get(f"{prefix}_p_top_1pct", 0.0)
        + 0.10 * metrics.get(f"{prefix}_p_top_5pct", 0.0)
    )


def precision_at_k(y: np.ndarray, pred: np.ndarray, k: int) -> float:
    if len(y) == 0:
        return 0.0
    k = min(k, len(y))
    idx = np.argsort(pred)[-k:]
    return float(y[idx].mean())


def precision_at_fraction(y: np.ndarray, pred: np.ndarray, frac: float) -> float:
    return precision_at_k(y, pred, max(1, int(len(y) * frac)))


if __name__ == "__main__":
    raise SystemExit(main())
