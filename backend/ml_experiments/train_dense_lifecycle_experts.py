"""Build dense 15m lifecycle rows and train mode-specific top/short experts.

This is the next redesign after the sparse lifecycle experiments. It replays
every target lifecycle at 15m resolution and evaluates experts by first signal
per lifecycle using validation-derived thresholds.

The script is experiment-only. It does not modify production signal logic.

Example:
    python -m ml_experiments.train_dense_lifecycle_experts --rebuild --max-entries 0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_experiments.train_dump_5m_compare import FEATS, aggregate, compute_features_interval, iso_ms
from ml_experiments.train_dynamic_behavior_state import BEHAVIOR_ORDER, assign_behavior_states
from ml_experiments.train_lifecycle_models import (
    ENTRY_CONTEXT,
    configure_interval,
    context_since_entry,
    current_variant,
    hours_to_bars,
)

TARGET_MODES = ["fast_dump", "slow_distribution", "second_distribution"]
HORIZONS = {
    "1h": 4,
    "3h": 12,
    "6h": 24,
    "12h": 48,
    "24h": 96,
    "72h": 288,
}
DAY_MS = 86_400_000

META_COLS = {
    "symbol",
    "entry_time",
    "entry_time_iso",
    "decision_time",
    "decision_time_iso",
    "entry_price",
    "current_price",
    "family",
    "cluster",
    "linked_event_id",
    "row_id",
    "behavior_state",
    "target",
    "eligible",
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_interval(args.interval)
    dataset_path = Path(args.dataset_out)
    if args.rebuild or not dataset_path.exists():
        dense = build_dense_dataset(args)
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        dense.to_parquet(dataset_path, index=False)
    else:
        dense = pd.read_parquet(dataset_path)
    if dense.empty:
        raise SystemExit("dense dataset is empty")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    feature_cols = feature_columns(dense)
    results: dict[str, Any] = {
        "dataset": str(dataset_path),
        "label_mode": args.label_mode,
        "interval": args.interval,
        "bar_minutes": int(current_variant().interval_ms / 60_000),
        "label_definition": label_definition(args.label_mode),
        "rows": int(len(dense)),
        "lifecycles": int(dense[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "modes": TARGET_MODES,
        "feature_count": len(feature_cols),
        "profile": dataset_profile(dense),
        "experts": {},
    }
    for mode in TARGET_MODES:
        mode_result: dict[str, Any] = {"profile": mode_profile(dense[dense["family"] == mode]), "tasks": {}}
        for task in ("top_exit", "short_clean"):
            print(f"training {mode} {task}", flush=True)
            mode_result["tasks"][task] = train_expert(dense, mode, task, feature_cols, out_dir / "experts" / mode, args)
            print(json.dumps(short_summary(mode_result["tasks"][task]), ensure_ascii=False), flush=True)
        results["experts"][mode] = mode_result

    out_json = out_dir / "dense_lifecycle_experts.json"
    out_md = out_dir / "dense_lifecycle_experts.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train dense 15m lifecycle top/short experts.")
    parser.add_argument("--source", default=r"E:\2C2G\币安数据库")
    parser.add_argument("--entries", default="storage/ml/lifecycle/long_entries.parquet")
    parser.add_argument("--dataset-out", default="storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--out-dir", default="storage/ml/dense_lifecycle_experts")
    parser.add_argument("--horizon-hours", type=float, default=72.0)
    parser.add_argument("--max-entries", type=int, default=0, help="0 means all target-mode entries.")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--label-mode", choices=("fixed", "dynamic", "hybrid"), default="fixed")
    parser.add_argument("--min-train-rows", type=int, default=200)
    parser.add_argument("--min-train-positives", type=int, default=30)
    parser.add_argument("--interval", choices=("15m", "5m", "5m_scaled"), default="15m")
    return parser.parse_args(argv)


def build_dense_dataset(args: argparse.Namespace) -> pd.DataFrame:
    source = Path(args.source)
    if not (source / "klines").is_dir():
        raise SystemExit(f"missing klines directory: {source / 'klines'}")
    entries = pd.read_parquet(args.entries).copy()
    entries = entries[entries["family"].isin(TARGET_MODES)].sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    if args.max_entries > 0:
        entries = entries.head(args.max_entries).copy()
    files = {p.stem.upper(): p for p in (source / "klines").glob("*.parquet")}
    horizon_bars = hours_to_bars(args.horizon_hours)
    rows: list[dict[str, Any]] = []
    total_symbols = entries["symbol"].nunique()
    for i, (symbol, erows) in enumerate(entries.groupby("symbol", sort=True), 1):
        path = files.get(str(symbol).upper())
        if path is None:
            continue
        start = int(erows["entry_time"].min()) - 5 * DAY_MS
        end = int(erows["entry_time"].max()) + int(args.horizon_hours * 3_600_000) + DAY_MS
        try:
            bars = aggregate(path, start, end, current_variant())
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)
            continue
        if bars is None or len(bars) < 300:
            continue
        features = compute_features_interval(bars, current_variant())
        time_to_ix = {int(v): int(ix) for ix, v in enumerate(bars["b"].values)}
        for entry in erows.itertuples(index=False):
            entry_ix = time_to_ix.get(int(entry.entry_time))
            if entry_ix is None or entry_ix < 96:
                continue
            max_ix = min(len(bars) - 2, entry_ix + horizon_bars)
            for ix in range(entry_ix, max_ix + 1):
                if ix >= len(features) or features.iloc[ix][FEATS].isna().any():
                    continue
                row = build_dense_row(bars, features, entry, entry_ix, ix)
                if row is not None:
                    rows.append(row)
        if i % 25 == 0:
            print(f"dense loaded {i}/{total_symbols} symbols rows={len(rows)}", flush=True)
    dense = pd.DataFrame(rows)
    if dense.empty:
        return dense
    dense["behavior_state"] = assign_behavior_states(dense)
    for behavior in BEHAVIOR_ORDER:
        dense[f"behavior_{behavior}"] = (dense["behavior_state"] == behavior).astype("int8")
    return dense.sort_values(["entry_time", "symbol", "decision_time"]).reset_index(drop=True)


def build_dense_row(bars: pd.DataFrame, features: pd.DataFrame, entry: Any, entry_ix: int, ix: int) -> dict[str, Any] | None:
    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    ctx = context_since_entry(bars, entry_ix, ix)
    if any(not np.isfinite(v) for v in ctx.values()):
        return None
    price = float(close[ix])
    row = features.iloc[ix][FEATS].to_dict()
    row.update(ctx)
    row.update(path_metrics(price, high, low, ix))
    row.update(
        {
            "symbol": str(entry.symbol),
            "entry_time": int(entry.entry_time),
            "entry_time_iso": iso_ms(int(entry.entry_time)),
            "decision_time": int(bars["b"].iloc[ix]),
            "decision_time_iso": iso_ms(int(bars["b"].iloc[ix])),
            "entry_price": float(entry.entry_price),
            "current_price": price,
            "family": str(entry.family),
            "cluster": int(entry.cluster) if pd.notna(entry.cluster) else None,
            "linked_event_id": str(entry.linked_event_id) if pd.notna(entry.linked_event_id) else None,
            "row_id": f"{entry.symbol}-{int(entry.entry_time)}-{int(bars['b'].iloc[ix])}",
        }
    )
    return row


def path_metrics(price: float, high: np.ndarray, low: np.ndarray, ix: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, bars in HORIZONS.items():
        end = min(len(high), ix + bars + 1)
        if end <= ix + 1:
            out[f"future_up_{name}"] = np.nan
            out[f"future_drop_{name}"] = np.nan
            out[f"short_adverse_before_down5_{name}"] = np.nan
            out[f"minutes_to_down5_{name}"] = None
            continue
        h = high[ix + 1 : end]
        l = low[ix + 1 : end]
        out[f"future_up_{name}"] = float(np.max(h) / price - 1.0)
        out[f"future_drop_{name}"] = float(price / np.min(l) - 1.0)
        adv, minutes = adverse_before_down(price, high, low, ix, end, 0.05)
        out[f"short_adverse_before_down5_{name}"] = adv
        out[f"minutes_to_down5_{name}"] = minutes
    return out


def adverse_before_down(price: float, high: np.ndarray, low: np.ndarray, ix: int, end: int, drop: float) -> tuple[float, int | None]:
    target = price * (1.0 - drop)
    hit = np.where(low[ix + 1 : end] <= target)[0]
    if len(hit):
        first = int(hit[0]) + 1
        return float(np.max(high[ix + 1 : ix + first + 1]) / price - 1.0), int(first * 15)
    return float(np.max(high[ix + 1 : end]) / price - 1.0), None


def feature_columns(dense: pd.DataFrame) -> list[str]:
    allowed = set(FEATS + ENTRY_CONTEXT + [f"behavior_{x}" for x in BEHAVIOR_ORDER])
    return [c for c in dense.columns if c in allowed and pd.api.types.is_numeric_dtype(dense[c])]


def task_frame(dense: pd.DataFrame, mode: str, task: str, label_mode: str = "fixed") -> pd.DataFrame:
    rows = dense[dense["family"] == mode].copy()
    if label_mode == "fixed":
        eligible, target, label_cols = fixed_task_labels(rows, mode, task)
    elif label_mode == "dynamic":
        eligible, target, label_cols = dynamic_task_labels(rows, mode, task)
    else:
        raise ValueError(label_mode)
    out = rows[eligible].copy()
    out["target"] = target.loc[out.index].astype("int8")
    for name, values in label_cols.items():
        out[name] = values.loc[out.index]
    return out


def effective_label_mode(label_mode: str, mode: str) -> str:
    if label_mode == "hybrid":
        return "dynamic" if mode == "fast_dump" else "fixed"
    return label_mode


def fixed_task_labels(rows: pd.DataFrame, mode: str, task: str) -> tuple[pd.Series, pd.Series, dict[str, pd.Series]]:
    drawdown = -rows["ctx_drawdown_from_entry_high"]
    if task == "top_exit":
        if mode == "fast_dump":
            eligible = (rows["ctx_high_since_entry"] >= 0.12) & (rows["ctx_ret_since_entry"] >= 0.04)
            target = (rows["future_drop_24h"] >= 0.10) & (rows["future_up_6h"] <= 0.04) & (rows["future_up_24h"] <= 0.06)
        elif mode == "slow_distribution":
            eligible = (rows["ctx_high_since_entry"] >= 0.12) & (rows["ctx_ret_since_entry"] >= 0.03)
            target = (rows["future_drop_72h"] >= 0.12) & (rows["future_up_24h"] <= 0.08)
        else:
            eligible = (rows["ctx_high_since_entry"] >= 0.10) & (rows["ctx_ret_since_entry"] >= 0.02)
            target = (rows["future_drop_72h"] >= 0.10) & (rows["future_up_24h"] <= 0.08)
    elif task == "short_clean":
        if mode == "fast_dump":
            eligible = (rows["ctx_high_since_entry"] >= 0.12) & (drawdown >= 0.04)
            target = (rows["future_drop_6h"] >= 0.06) & (rows["short_adverse_before_down5_6h"] <= 0.04)
        elif mode == "slow_distribution":
            eligible = (rows["ctx_high_since_entry"] >= 0.12) & (drawdown >= 0.05)
            target = (rows["future_drop_24h"] >= 0.08) & (rows["short_adverse_before_down5_24h"] <= 0.06)
        else:
            eligible = (rows["ctx_high_since_entry"] >= 0.10) & (drawdown >= 0.04)
            target = (rows["future_drop_24h"] >= 0.08) & (rows["short_adverse_before_down5_24h"] <= 0.05)
    else:
        raise ValueError(task)
    return eligible.astype(bool), target.astype(bool), {}


def dynamic_task_labels(rows: pd.DataFrame, mode: str, task: str) -> tuple[pd.Series, pd.Series, dict[str, pd.Series]]:
    """Create row-specific labels from past-only context plus future path.

    The thresholds are dynamic, but they are not features. They use only the
    current closed-candle context to scale the future target/adverse criteria:
    bigger pumps must offer bigger follow-through, and high-noise names get a
    slightly wider but capped adverse allowance.
    """
    amp = pd.to_numeric(rows["ctx_high_since_entry"], errors="coerce").clip(lower=0.0)
    ret = pd.to_numeric(rows["ctx_ret_since_entry"], errors="coerce")
    drawdown = (-pd.to_numeric(rows["ctx_drawdown_from_entry_high"], errors="coerce")).clip(lower=0.0)
    noise = dynamic_noise(rows)

    if mode == "fast_dump":
        min_amp = clip_series(0.08 + 2.5 * noise, 0.12, 0.25)
        max_top_drawdown = clip_series(0.28 * amp + 2.0 * noise, 0.07, 0.22)
        if task == "top_exit":
            drop_target = clip_series(0.14 * amp + 1.5 * noise, 0.08, 0.32)
            adverse_6h = clip_series(1.3 * noise, 0.025, 0.07)
            adverse_24h = clip_series(1.9 * noise, 0.035, 0.10)
            eligible = (amp >= min_amp) & (ret >= 0.0) & (drawdown <= max_top_drawdown)
            target = (
                (rows["future_drop_24h"] >= drop_target)
                & (rows["future_up_6h"] <= adverse_6h)
                & (rows["future_up_24h"] <= adverse_24h)
            )
            label_cols = {
                "label_drop_target": drop_target,
                "label_adverse_cap_6h": adverse_6h,
                "label_adverse_cap_24h": adverse_24h,
                "label_min_amp": min_amp,
                "label_max_top_drawdown": max_top_drawdown,
            }
        elif task == "short_clean":
            min_drawdown = clip_series(0.10 * amp + 1.2 * noise, 0.035, 0.14)
            drop_target = clip_series(0.10 * amp + 1.5 * noise, 0.055, 0.22)
            adverse_cap = clip_series(1.4 * noise, 0.025, 0.065)
            eligible = (amp >= min_amp) & (drawdown >= min_drawdown)
            target = (rows["future_drop_6h"] >= drop_target) & (rows["short_adverse_before_down5_6h"] <= adverse_cap)
            label_cols = {
                "label_drop_target": drop_target,
                "label_adverse_cap_6h": adverse_cap,
                "label_min_amp": min_amp,
                "label_min_drawdown": min_drawdown,
            }
        else:
            raise ValueError(task)
    elif mode == "slow_distribution":
        min_amp = clip_series(0.06 + 2.0 * noise, 0.10, 0.20)
        max_top_drawdown = clip_series(0.35 * amp + 2.0 * noise, 0.08, 0.25)
        if task == "top_exit":
            drop_target = clip_series(0.10 * amp + 1.7 * noise, 0.07, 0.22)
            adverse_24h = clip_series(2.2 * noise, 0.05, 0.13)
            eligible = (amp >= min_amp) & (ret >= -0.03) & (drawdown <= max_top_drawdown)
            target = (rows["future_drop_72h"] >= drop_target) & (rows["future_up_24h"] <= adverse_24h)
            label_cols = {
                "label_drop_target": drop_target,
                "label_adverse_cap_24h": adverse_24h,
                "label_min_amp": min_amp,
                "label_max_top_drawdown": max_top_drawdown,
            }
        elif task == "short_clean":
            min_drawdown = clip_series(0.13 * amp + 1.3 * noise, 0.04, 0.13)
            drop_target = clip_series(0.09 * amp + 1.8 * noise, 0.06, 0.18)
            adverse_cap = clip_series(2.0 * noise, 0.04, 0.10)
            eligible = (amp >= min_amp) & (drawdown >= min_drawdown)
            target = (rows["future_drop_24h"] >= drop_target) & (rows["short_adverse_before_down5_24h"] <= adverse_cap)
            label_cols = {
                "label_drop_target": drop_target,
                "label_adverse_cap_24h": adverse_cap,
                "label_min_amp": min_amp,
                "label_min_drawdown": min_drawdown,
            }
        else:
            raise ValueError(task)
    elif mode == "second_distribution":
        min_amp = clip_series(0.05 + 1.8 * noise, 0.08, 0.16)
        max_top_drawdown = clip_series(0.40 * amp + 2.0 * noise, 0.06, 0.20)
        if task == "top_exit":
            drop_target = clip_series(0.11 * amp + 1.8 * noise, 0.06, 0.18)
            adverse_24h = clip_series(2.0 * noise, 0.045, 0.11)
            eligible = (amp >= min_amp) & (ret >= -0.03) & (drawdown <= max_top_drawdown)
            target = (rows["future_drop_72h"] >= drop_target) & (rows["future_up_24h"] <= adverse_24h)
            label_cols = {
                "label_drop_target": drop_target,
                "label_adverse_cap_24h": adverse_24h,
                "label_min_amp": min_amp,
                "label_max_top_drawdown": max_top_drawdown,
            }
        elif task == "short_clean":
            min_drawdown = clip_series(0.12 * amp + 1.2 * noise, 0.035, 0.11)
            drop_target = clip_series(0.10 * amp + 1.6 * noise, 0.055, 0.16)
            adverse_cap = clip_series(1.8 * noise, 0.035, 0.08)
            eligible = (amp >= min_amp) & (drawdown >= min_drawdown)
            target = (rows["future_drop_24h"] >= drop_target) & (rows["short_adverse_before_down5_24h"] <= adverse_cap)
            label_cols = {
                "label_drop_target": drop_target,
                "label_adverse_cap_24h": adverse_cap,
                "label_min_amp": min_amp,
                "label_min_drawdown": min_drawdown,
            }
        else:
            raise ValueError(task)
    else:
        raise ValueError(mode)
    return eligible.astype(bool), target.astype(bool), label_cols


def dynamic_noise(rows: pd.DataFrame) -> pd.Series:
    atr = pd.to_numeric(rows["atr_14"], errors="coerce").fillna(0.0)
    retstd = pd.to_numeric(rows["retstd_20"], errors="coerce").fillna(0.0) * 1.5
    return clip_series(pd.Series(np.maximum(atr.to_numpy(), retstd.to_numpy()), index=rows.index), 0.006, 0.10)


def clip_series(values: pd.Series | np.ndarray | float, lower: float, upper: float) -> pd.Series:
    if isinstance(values, pd.Series):
        index = values.index
        arr = values.to_numpy(dtype=float)
    else:
        arr = np.asarray(values, dtype=float)
        index = None
    clipped = np.clip(arr, lower, upper)
    return pd.Series(clipped, index=index)


def train_expert(dense: pd.DataFrame, mode: str, task: str, feature_cols: list[str], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    effective_labels = effective_label_mode(args.label_mode, mode)
    data = task_frame(dense, mode, task, effective_labels)
    if len(data) < args.min_train_rows:
        return {
            "mode": mode,
            "task": task,
            "label_mode": args.label_mode,
            "effective_label_mode": effective_labels,
            "skipped": "rows_too_small",
            "rows": int(len(data)),
        }
    if int(data["target"].sum()) < args.min_train_positives:
        return {
            "mode": mode,
            "task": task,
            "label_mode": args.label_mode,
            "effective_label_mode": effective_labels,
            "skipped": "positives_too_small",
            "rows": int(len(data)),
            "positives": int(data["target"].sum()),
        }
    split = lifecycle_split(data)
    train = data.iloc[split["train"]]
    val = data.iloc[split["val"]]
    test = data.iloc[split["test"]]
    if train.empty or val.empty or test.empty:
        return {
            "mode": mode,
            "task": task,
            "label_mode": args.label_mode,
            "effective_label_mode": effective_labels,
            "skipped": "empty_split",
            "rows": int(len(data)),
            "split": split_summary(data, split),
        }
    pos = int(train["target"].sum())
    neg = int(len(train) - pos)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=450,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=25,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=2.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model.fit(train[feature_cols], train["target"])
    val_score = model.predict_proba(val[feature_cols])[:, 1]
    test_score = model.predict_proba(test[feature_cols])[:, 1]
    val_metrics = row_metrics(val, val_score)
    test_metrics = row_metrics(test, test_score)
    thresholds = {
        f"q{int(q * 100)}": float(np.quantile(val_score, q))
        for q in (0.80, 0.90, 0.95)
        if len(val_score)
    }
    replay = gated_first_signal_replay(test, test_score, thresholds, task)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / f"{task}_{args.label_mode}.txt"
    model.booster_.save_model(str(model_path))
    result = {
        "mode": mode,
        "task": task,
        "label_mode": args.label_mode,
        "effective_label_mode": effective_labels,
        "rows": int(len(data)),
        "positives": int(data["target"].sum()),
        "positive_rate": float(data["target"].mean()),
        "label_stats": label_stats(data),
        "split": split_summary(data, split),
        "val": val_metrics,
        "test": test_metrics,
        "thresholds_from_val": {k: round(v, 6) for k, v in thresholds.items()},
        "test_first_signal": replay,
        "feature_importance": feature_importance(model, feature_cols),
        "model_path": str(model_path),
    }
    (out_dir / f"{task}_{args.label_mode}_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def lifecycle_split(data: pd.DataFrame) -> dict[str, np.ndarray]:
    keys = data[["symbol", "entry_time"]].drop_duplicates().sort_values("entry_time").reset_index(drop=True)
    unique_times = np.sort(keys["entry_time"].unique())
    q70, q85 = np.quantile(unique_times, [0.70, 0.85])
    embargo = 3 * DAY_MS
    train_keys = keys[keys["entry_time"] <= q70]
    val_keys = keys[(keys["entry_time"] >= q70 + embargo) & (keys["entry_time"] <= q85)]
    test_keys = keys[keys["entry_time"] >= q85 + embargo]
    key_series = data["symbol"].astype(str) + "|" + data["entry_time"].astype("int64").astype(str)
    return {
        "train": np.flatnonzero(key_series.isin(to_key_set(train_keys)).to_numpy()),
        "val": np.flatnonzero(key_series.isin(to_key_set(val_keys)).to_numpy()),
        "test": np.flatnonzero(key_series.isin(to_key_set(test_keys)).to_numpy()),
    }


def to_key_set(keys: pd.DataFrame) -> set[str]:
    return set(keys["symbol"].astype(str) + "|" + keys["entry_time"].astype("int64").astype(str))


def row_metrics(rows: pd.DataFrame, score: np.ndarray) -> dict[str, Any]:
    if rows.empty:
        return {"rows": 0}
    y = rows["target"].astype(int).to_numpy()
    out: dict[str, Any] = {
        "rows": int(len(rows)),
        "lifecycles": int(rows[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "base_rate": float(y.mean()),
        "auc": maybe_auc(y, score),
        "ap": maybe_ap(y, score),
    }
    for q in (0.80, 0.90, 0.95):
        out[f"q{int(q * 100)}"] = threshold_row_metrics(rows, y, score, float(np.quantile(score, q)))
    return out


def maybe_auc(y: np.ndarray, score: np.ndarray) -> float | None:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, score))


def maybe_ap(y: np.ndarray, score: np.ndarray) -> float | None:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    from sklearn.metrics import average_precision_score

    return float(average_precision_score(y, score))


def threshold_row_metrics(rows: pd.DataFrame, y: np.ndarray, score: np.ndarray, threshold: float) -> dict[str, Any]:
    selected = score >= threshold
    grp = rows.iloc[np.flatnonzero(selected)]
    return {
        "threshold": round(float(threshold), 6),
        "selected": int(selected.sum()),
        "precision": float(y[selected].mean()) if int(selected.sum()) else None,
        "lifecycles": int(grp[["symbol", "entry_time"]].drop_duplicates().shape[0]) if len(grp) else 0,
    }


def gated_first_signal_replay(rows: pd.DataFrame, score: np.ndarray, thresholds: dict[str, float], task: str) -> dict[str, Any]:
    gates = {
        "all": None,
        "breakdown": {"breakdown"},
        "breakdown_pullback": {"breakdown", "pullback_risk"},
        "distribution_climax_pullback": {"distribution", "climax_risk", "pullback_risk"},
    }
    out: dict[str, Any] = {}
    for gate_name, states in gates.items():
        if states is None:
            gate_rows = rows
            gate_score = score
        else:
            mask = rows["behavior_state"].isin(states).to_numpy()
            gate_rows = rows.iloc[np.flatnonzero(mask)]
            gate_score = score[mask]
        out[gate_name] = {
            name: first_signal_replay(gate_rows, gate_score, threshold, task)
            for name, threshold in thresholds.items()
        }
    return out


def first_signal_replay(rows: pd.DataFrame, score: np.ndarray, threshold: float, task: str) -> dict[str, Any]:
    if rows.empty:
        return {"lifecycles": 0, "triggered": 0}
    replay = rows.copy()
    replay["score"] = score
    total_events = replay[["symbol", "entry_time"]].drop_duplicates().shape[0]
    triggered = replay[replay["score"] >= threshold].sort_values(["symbol", "entry_time", "decision_time"])
    first = triggered.groupby(["symbol", "entry_time"], as_index=False).first()
    if first.empty:
        return {"threshold": round(float(threshold), 6), "lifecycles": int(total_events), "triggered": 0, "coverage": 0.0}
    y = first["target"].astype(int).to_numpy()
    payload = {
        "threshold": round(float(threshold), 6),
        "lifecycles": int(total_events),
        "triggered": int(len(first)),
        "coverage": float(len(first) / total_events) if total_events else None,
        "precision": float(y.mean()) if len(y) else None,
        "median_delay_hours": safe_median_series((first["decision_time"] - first["entry_time"]) / 3_600_000),
        "median_future_up_6h": safe_median(first, "future_up_6h"),
        "median_future_up_24h": safe_median(first, "future_up_24h"),
        "median_future_drop_3h": safe_median(first, "future_drop_3h"),
        "median_future_drop_6h": safe_median(first, "future_drop_6h"),
        "median_future_drop_12h": safe_median(first, "future_drop_12h"),
        "median_future_drop_24h": safe_median(first, "future_drop_24h"),
        "median_future_drop_72h": safe_median(first, "future_drop_72h"),
        "behavior_mix": {str(k): int(v) for k, v in first["behavior_state"].value_counts().to_dict().items()},
    }
    if task == "short_clean":
        payload["median_short_adverse_6h"] = safe_median(first, "short_adverse_before_down5_6h")
        payload["median_short_adverse_24h"] = safe_median(first, "short_adverse_before_down5_24h")
    else:
        payload["median_top_adverse_up_24h"] = safe_median(first, "future_up_24h")
    return payload


def split_summary(data: pd.DataFrame, split: dict[str, np.ndarray]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, idx in split.items():
        part = data.iloc[idx]
        out[name] = {
            "rows": int(len(part)),
            "lifecycles": int(part[["symbol", "entry_time"]].drop_duplicates().shape[0]) if len(part) else 0,
            "positives": int(part["target"].sum()) if len(part) else 0,
            "positive_rate": float(part["target"].mean()) if len(part) else None,
        }
    return out


def feature_importance(model: lgb.LGBMClassifier, cols: list[str], n: int = 25) -> list[dict[str, Any]]:
    gain = model.booster_.feature_importance(importance_type="gain")
    order = np.argsort(gain)[::-1][:n]
    return [{"feature": cols[int(i)], "gain": float(gain[int(i)])} for i in order]


def label_stats(data: pd.DataFrame) -> dict[str, Any]:
    cols = [c for c in data.columns if c.startswith("label_")]
    stats: dict[str, Any] = {}
    for col in cols:
        s = pd.to_numeric(data[col], errors="coerce").dropna()
        if s.empty:
            continue
        stats[col] = {
            "p25": float(s.quantile(0.25)),
            "median": float(s.quantile(0.50)),
            "p75": float(s.quantile(0.75)),
            "p90": float(s.quantile(0.90)),
        }
    return stats


def label_definition(label_mode: str) -> dict[str, Any]:
    if label_mode == "fixed":
        return {
            "summary": "Original fixed thresholds per mode/task.",
            "fast_dump_top_exit": "future_drop_24h>=10%, future_up_6h<=4%, future_up_24h<=6%",
            "fast_dump_short_clean": "future_drop_6h>=6%, adverse_before_down5_6h<=4%",
            "slow_distribution_top_exit": "future_drop_72h>=12%, future_up_24h<=8%",
            "slow_distribution_short_clean": "future_drop_24h>=8%, adverse_before_down5_24h<=6%",
        }
    if label_mode == "dynamic":
        return {
            "summary": "Row-specific thresholds scaled by closed-candle pump amplitude, drawdown, ATR/return volatility, and mode.",
            "principle": "Bigger/noisier pumps require larger follow-through; adverse allowance widens with noise but is capped.",
            "no_future_features": "Dynamic thresholds use past/current context only; future path is used only to mark labels and evaluate replay.",
        }
    if label_mode == "hybrid":
        return {
            "summary": "Use dynamic labels for fast_dump and fixed labels for slow_distribution/second_distribution.",
            "reason": "Latest replay shows fast_dump benefits from scaled targets while slow_distribution degrades under the current dynamic definition.",
            "no_future_features": "All label thresholds use past/current context only; future path is used only to mark labels and evaluate replay.",
        }
    raise ValueError(label_mode)


def dataset_profile(dense: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(dense)),
        "lifecycles": int(dense[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "family_counts": {str(k): int(v) for k, v in dense["family"].value_counts().to_dict().items()},
        "behavior_counts": {str(k): int(v) for k, v in dense["behavior_state"].value_counts().to_dict().items()},
        "median_rows_per_lifecycle": safe_median_series(dense.groupby(["symbol", "entry_time"]).size()),
    }


def mode_profile(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {"rows": 0, "lifecycles": 0}
    return {
        "rows": int(len(rows)),
        "lifecycles": int(rows[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "behavior_counts": {str(k): int(v) for k, v in rows["behavior_state"].value_counts().to_dict().items()},
        "median_high_since_entry": safe_median(rows, "ctx_high_since_entry"),
        "median_max_drawdown_from_high": safe_median_series(-rows["ctx_drawdown_from_entry_high"]),
    }


def short_summary(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("skipped"):
        return result
    replay = result["test_first_signal"].get("all", {}).get("q90")
    return {
        "mode": result["mode"],
        "task": result["task"],
        "rows": result["rows"],
        "test_auc": result["test"].get("auc"),
        "test_q90_precision": result["test"].get("q90", {}).get("precision"),
        "first_signal_q90": replay,
    }


def safe_median(df: pd.DataFrame, col: str) -> float | None:
    if df.empty or col not in df:
        return None
    value = float(pd.to_numeric(df[col], errors="coerce").median())
    return value if np.isfinite(value) else None


def safe_median_series(series: pd.Series) -> float | None:
    if len(series) == 0:
        return None
    value = float(pd.to_numeric(series, errors="coerce").median())
    return value if np.isfinite(value) else None


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Dense 15m Lifecycle Experts",
        "",
        "Per-mode top/short experts trained on dense 15m lifecycle replay rows.",
        "",
        f"- Dataset: `{results['dataset']}`",
        f"- Label mode: `{results.get('label_mode', 'fixed')}`",
        f"- Rows: {results['rows']}",
        f"- Lifecycles: {results['lifecycles']}",
        f"- Feature count: {results['feature_count']}",
        "",
        "## Dataset Profile",
        "",
        f"- Family counts: `{json.dumps(results['profile']['family_counts'], ensure_ascii=False)}`",
        f"- Behavior counts: `{json.dumps(results['profile']['behavior_counts'], ensure_ascii=False)}`",
        f"- Median rows per lifecycle: {num(results['profile'].get('median_rows_per_lifecycle'))}",
        "",
        "## Expert Row Metrics",
        "",
        "| Mode | Task | Rows | Test Rows | Base | AUC | q90 Precision | q95 Precision |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, mode_result in results["experts"].items():
        for task, result in mode_result["tasks"].items():
            if result.get("skipped"):
                lines.append(f"| {mode} | {task} | skipped: {result['skipped']} | - | - | - | - | - |")
                continue
            test = result["test"]
            lines.append(
                f"| {mode} | {task} | {result['rows']} | {test.get('rows', 0)} | {pct(test.get('base_rate'))} | "
                f"{num(test.get('auc'))} | {pct(test.get('q90', {}).get('precision'))} | {pct(test.get('q95', {}).get('precision'))} |"
            )

    lines += [
        "",
        "## First-Signal Replay Using Validation Thresholds",
        "",
        "| Mode | Task | Threshold | Coverage | Precision | Delay h | Up24 | Drop6 | Drop12 | Drop24 | Drop72 | Short Adv 6h | Short Adv 24h | Behavior Mix |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for mode, mode_result in results["experts"].items():
        for task, result in mode_result["tasks"].items():
            if result.get("skipped"):
                continue
            for gate_name, gate_result in result["test_first_signal"].items():
                for threshold_name in ("q80", "q90", "q95"):
                    replay = gate_result.get(threshold_name, {})
                    lines.append(
                        f"| {mode} | {task} | {gate_name}/{threshold_name} | {pct(replay.get('coverage'))} | {pct(replay.get('precision'))} | "
                        f"{num(replay.get('median_delay_hours'))} | {pct(replay.get('median_future_up_24h'))} | "
                        f"{pct(replay.get('median_future_drop_6h'))} | {pct(replay.get('median_future_drop_12h'))} | "
                        f"{pct(replay.get('median_future_drop_24h'))} | {pct(replay.get('median_future_drop_72h'))} | "
                        f"{pct(replay.get('median_short_adverse_6h'))} | {pct(replay.get('median_short_adverse_24h'))} | "
                        f"{json.dumps(replay.get('behavior_mix', {}), ensure_ascii=False)} |"
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
