from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml_experiments.train_sequence_models import build_model, ModelSpec


def main() -> int:
    args = parse_args()
    bundle = torch.load(args.model, map_location="cpu", weights_only=False)
    spec = ModelSpec(str(bundle["model_name"]), str(bundle["model_kind"]), dict(bundle["model_params"]))
    model = build_model(spec, len(bundle["feature_names"]), int(bundle["seq_len"]))
    model.load_state_dict(bundle["state_dict"])
    model.eval()

    data = np.load(args.input, allow_pickle=True)
    x = data[args.array].astype(np.float32)
    mean = np.asarray(bundle["mean"], dtype=np.float32)
    std = np.asarray(bundle["std"], dtype=np.float32)
    x = (x - mean.reshape(1, 1, -1)) / std.reshape(1, 1, -1)
    x[~np.isfinite(x)] = 0.0

    with torch.no_grad():
        pred = torch.sigmoid(model(torch.from_numpy(x)).squeeze(-1)).cpu().numpy()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.endswith(".json"):
        payload = {"model": str(args.model), "scores": pred.tolist()}
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    else:
        np.save(out, pred)
    print(f"saved {len(pred)} scores -> {out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local inference with a trained sequence model bundle.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--input", required=True, help="NPZ containing sequence array.")
    parser.add_argument("--array", default="x")
    parser.add_argument("--out", default="storage/ml/predictions.npy")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
