"""Train high-pump lifecycle experts from the first 40% pump crossing.

This experiment tests a stricter lifecycle definition:

- first wait until an existing pump lifecycle reaches a configured high-gain
  threshold, default 40%;
- reset the lifecycle clock/context at that crossing;
- train high-zone top and breakdown short experts that do not depend on the
  family router being confident yet.

It is experiment-only and does not modify production model files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from ml_experiments.backtest_lifecycle_router_replay import add_slow_features_vectorized, prepare_dense, split_times
from pump_dump_hunter.ml import lifecycle as life


DAY_MS = 86_400_000
BAR_HOURS = 0.25
META = {
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
    "life_id",
    "behavior_state",
    "target",
    "eligible",
}
ORIG_CONTEXT = [
    "orig_ctx_ret_since_entry",
    "orig_ctx_high_since_entry",
    "orig_ctx_low_since_entry",
    "orig_ctx_drawdown_from_entry_high",
    "orig_ctx_hours_since_entry",
    "high40_cross_orig_gain",
]
HORIZONS = ("1h", "3h", "6h", "12h", "24h", "72h")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dense = prepare_dense(pd.read_parquet(args.dense).copy())
    high = build_high_pump_dataset(dense, min_gain=args.min_gain)
    if high.empty:
        raise SystemExit("no high-pump rows built")
    high_path = out_dir / f"high_pump_{int(args.min_gain * 100)}_dense.parquet"
    high.to_parquet(high_path, index=False)

    feature_cols = feature_columns(high)
    results: dict[str, Any] = {
        "dataset": args.dense,
        "high_dataset": str(high_path),
        "min_gain": args.min_gain,
        "rows": int(len(high)),
        "lifecycles": int(high["life_id"].nunique()),
        "feature_count": len(feature_cols),
        "profile": dataset_profile(high),
        "tasks": {},
    }
    task_defs = {
        "high_top": make_high_top_frame,
        "high_short": make_high_short_frame,
    }
    for task, builder in task_defs.items():
        print(f"training {task}", flush=True)
        task_rows = builder(high, args)
        results["tasks"][task] = train_task(task, task_rows, feature_cols, out_dir / "models", args)
        print(json.dumps(compact_task_summary(results["tasks"][task]), ensure_ascii=False), flush=True)

    out_json = out_dir / "high_pump40_experts.json"
    out_md = out_dir / "high_pump40_experts.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md), "dataset": str(high_path)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train high-pump 40% top/short experts.")
    parser.add_argument("--dense", default="backend/storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--out-dir", default="backend/storage/ml/high_pump40_experts")
    parser.add_argument("--min-gain", type=float, default=0.40)
    parser.add_argument("--min-train-rows", type=int, default=200)
    parser.add_argument("--min-train-positives", type=int, default=30)
    parser.add_argument("--top-label", choices=("top15", "clean12"), default="top15")
    parser.add_argument("--short-label", choices=("short6", "short12"), default="short12")
    return parser.parse_args(argv)


def build_high_pump_dataset(dense: pd.DataFrame, min_gain: float) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    dense = dense.sort_values(["life_id", "decision_time"]).copy()
    for life_id, group in dense.groupby("life_id", sort=False):
        group = group.sort_values("decision_time").copy()
        cross = group[group["ctx_high_since_entry"] >= min_gain]
        if cross.empty:
            continue
        cross_idx = cross.index[0]
        start_pos = int(np.flatnonzero(group.index.to_numpy() == cross_idx)[0])
        sub = group.iloc[start_pos:].copy()
        if len(sub) < 8:
            continue
        entry_price = float(sub.iloc[0]["current_price"])
        if not np.isfinite(entry_price) or entry_price <= 0:
            continue
        price = pd.to_numeric(sub["current_price"], errors="coerce").to_numpy(float)
        cum_high = np.maximum.accumulate(price)
        cum_low = np.minimum.accumulate(price)
        bars = np.arange(len(sub), dtype=float)

        for col in life.ENTRY_CONTEXT:
            sub[f"orig_{col}"] = sub[col]
        sub["source_life_id"] = str(life_id)
        sub["life_id"] = str(life_id) + f"|high{int(min_gain * 100)}"
        sub["entry_time"] = int(sub.iloc[0]["decision_time"])
        sub["entry_time_iso"] = str(sub.iloc[0]["decision_time_iso"])
        sub["entry_price"] = entry_price
        sub["high40_cross_orig_gain"] = float(sub.iloc[0]["orig_ctx_high_since_entry"])
        sub["ctx_bars_since_entry"] = bars
        sub["ctx_hours_since_entry"] = bars * BAR_HOURS
        sub["ctx_ret_since_entry"] = price / entry_price - 1.0
        sub["ctx_high_since_entry"] = cum_high / entry_price - 1.0
        sub["ctx_low_since_entry"] = cum_low / entry_price - 1.0
        sub["ctx_drawdown_from_entry_high"] = price / cum_high - 1.0
        sub["ctx_new_high_since_entry"] = (cum_high > price[0] * 1.001).astype(float)
        sub["row_id"] = (
            sub["symbol"].astype(str)
            + "-"
            + sub["entry_time"].astype("int64").astype(str)
            + "-"
            + sub["decision_time"].astype("int64").astype(str)
        )
        states = sub.apply(life.assign_behavior_state, axis=1)
        sub["behavior_state"] = states
        for behavior in life.BEHAVIOR_ORDER:
            sub[f"behavior_{behavior}"] = (states == behavior).astype("int8")
        rows.append(sub)
    if not rows:
        return pd.DataFrame()
    high = pd.concat(rows, ignore_index=True).sort_values(["entry_time", "symbol", "decision_time"]).reset_index(drop=True)
    add_slow_features_vectorized(high)
    return high


def feature_columns(rows: pd.DataFrame) -> list[str]:
    allowed = set(life.FAST_FEATURES + life.SLOW_DERIVED + ORIG_CONTEXT)
    blocked_prefixes = ("future_", "minutes_to_")
    cols = []
    for col in rows.columns:
        if col in allowed and pd.api.types.is_numeric_dtype(rows[col]) and not col.startswith(blocked_prefixes):
            cols.append(col)
    return cols


def make_high_top_frame(rows: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    drawdown = (-pd.to_numeric(rows["ctx_drawdown_from_entry_high"], errors="coerce")).clip(lower=0.0)
    orig_drawdown = (-pd.to_numeric(rows["orig_ctx_drawdown_from_entry_high"], errors="coerce")).clip(lower=0.0)
    setup = (
        rows["behavior_state"].isin(["acceleration", "trend_hold", "climax_risk", "distribution"])
        & (drawdown <= 0.12)
        & (pd.to_numeric(rows["ctx_ret_since_entry"], errors="coerce") >= -0.08)
        & (pd.to_numeric(rows["ret_3"], errors="coerce") >= -0.06)
        & (orig_drawdown <= 0.22)
    )
    if args.top_label == "top15":
        target = (
            (rows["future_drop_24h"] >= 0.15)
            & (rows["future_up_6h"] <= 0.08)
            & (rows["future_up_24h"] <= 0.16)
        )
    else:
        target = (
            (rows["future_drop_24h"] >= 0.12)
            & (rows["short_adverse_before_down5_24h"] <= 0.06)
            & (rows["future_drop_6h"] >= 0.04)
        )
    out = rows[setup].copy()
    out["target"] = target.loc[out.index].astype("int8")
    out["eligible"] = 1
    return out


def make_high_short_frame(rows: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    drawdown = (-pd.to_numeric(rows["ctx_drawdown_from_entry_high"], errors="coerce")).clip(lower=0.0)
    orig_drawdown = (-pd.to_numeric(rows["orig_ctx_drawdown_from_entry_high"], errors="coerce")).clip(lower=0.0)
    weak = (
        (pd.to_numeric(rows["ret_3"], errors="coerce") <= -0.025)
        | (pd.to_numeric(rows["ret_6"], errors="coerce") <= -0.040)
        | (pd.to_numeric(rows["dist_ema21"], errors="coerce") <= -0.020)
    )
    setup = (
        rows["behavior_state"].isin(["pullback_risk", "breakdown", "distribution", "climax_risk"])
        & ((drawdown >= 0.035) | (orig_drawdown >= 0.06))
        & weak
    )
    if args.short_label == "short6":
        target = (rows["future_drop_6h"] >= 0.06) & (rows["short_adverse_before_down5_6h"] <= 0.045)
    else:
        target = (
            (rows["future_drop_12h"] >= 0.06)
            & (rows["future_drop_24h"] >= 0.10)
            & (rows["short_adverse_before_down5_24h"] <= 0.06)
        )
    out = rows[setup].copy()
    out["target"] = target.loc[out.index].astype("int8")
    out["eligible"] = 1
    return out


def train_task(task: str, data: pd.DataFrame, feature_cols: list[str], model_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task": task,
        "rows": int(len(data)),
        "lifecycles": int(data["life_id"].nunique()) if len(data) else 0,
        "positive_rate": float(data["target"].mean()) if len(data) else None,
        "label": args.top_label if task == "high_top" else args.short_label,
    }
    data = data.dropna(subset=feature_cols + ["target"]).copy()
    if len(data) < args.min_train_rows:
        return {**result, "skipped": "rows_too_small"}
    if int(data["target"].sum()) < args.min_train_positives:
        return {**result, "skipped": "positives_too_small", "positives": int(data["target"].sum())}
    split = lifecycle_split(data)
    train = data.iloc[split["train"]]
    val = data.iloc[split["val"]]
    test = data.iloc[split["test"]]
    if train.empty or val.empty or test.empty or train["target"].nunique() < 2:
        return {**result, "skipped": "empty_or_one_class_split", "split": split_summary(data, split)}
    pos = int(train["target"].sum())
    neg = int(len(train) - pos)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=500,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=25,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=2.5,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=17,
        verbosity=-1,
    )
    model.fit(
        train[feature_cols],
        train["target"],
        eval_set=[(val[feature_cols], val["target"])],
        eval_metric="auc",
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    val_score = model.predict_proba(val[feature_cols])[:, 1]
    test_score = model.predict_proba(test[feature_cols])[:, 1]
    thresholds = {f"q{int(q * 100)}": float(np.nanquantile(val_score, q)) for q in (0.80, 0.85, 0.90, 0.95, 0.98)}
    replay = {name: first_signal_metrics(test, test_score, threshold) for name, threshold in thresholds.items()}
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{task}_{result['label']}.txt"
    model.booster_.save_model(str(model_path))
    return {
        **result,
        "rows": int(len(data)),
        "positives": int(data["target"].sum()),
        "positive_rate": float(data["target"].mean()),
        "feature_count": len(feature_cols),
        "split": split_summary(data, split),
        "val_auc": maybe_auc(val["target"], val_score),
        "test_auc": maybe_auc(test["target"], test_score),
        "val_ap": maybe_ap(val["target"], val_score),
        "test_ap": maybe_ap(test["target"], test_score),
        "thresholds_from_val": {k: round(v, 6) for k, v in thresholds.items()},
        "test_first_signal": replay,
        "best": best_replay(replay),
        "feature_importance": feature_importance(model, feature_cols),
        "model_path": str(model_path),
    }


def lifecycle_split(data: pd.DataFrame) -> dict[str, np.ndarray]:
    keys = data[["life_id", "entry_time"]].drop_duplicates().sort_values("entry_time").reset_index(drop=True)
    unique_times = np.sort(keys["entry_time"].unique())
    q70, q85 = np.quantile(unique_times, [0.70, 0.85])
    embargo = 3 * DAY_MS
    train_ids = set(keys[keys["entry_time"] <= q70]["life_id"].astype(str))
    val_ids = set(keys[(keys["entry_time"] >= q70 + embargo) & (keys["entry_time"] <= q85)]["life_id"].astype(str))
    test_ids = set(keys[keys["entry_time"] >= q85 + embargo]["life_id"].astype(str))
    ids = data["life_id"].astype(str)
    return {
        "train": np.flatnonzero(ids.isin(train_ids).to_numpy()),
        "val": np.flatnonzero(ids.isin(val_ids).to_numpy()),
        "test": np.flatnonzero(ids.isin(test_ids).to_numpy()),
    }


def first_signal_metrics(rows: pd.DataFrame, score: np.ndarray, threshold: float) -> dict[str, Any]:
    if rows.empty:
        return {"signals": 0}
    frame = rows.copy()
    frame["score"] = score
    total = int(frame["life_id"].nunique())
    selected = frame[frame["score"] >= threshold].sort_values(["life_id", "decision_time"])
    first = selected.groupby("life_id", as_index=False).first()
    if first.empty:
        return {"threshold": round(float(threshold), 6), "lifecycles": total, "signals": 0, "coverage": 0.0}
    peak = peak_frame(rows)
    first = first.merge(peak, on="life_id", how="left")
    first["delay_from_peak_h"] = (first["decision_time"] - first["peak_time"]) / 3_600_000
    return {
        "threshold": round(float(threshold), 6),
        "lifecycles": total,
        "signals": int(len(first)),
        "coverage": float(len(first) / total) if total else None,
        "precision": float(first["target"].mean()),
        "future_drop_6h_med": safe_median(first, "future_drop_6h"),
        "future_drop_12h_med": safe_median(first, "future_drop_12h"),
        "future_drop_24h_med": safe_median(first, "future_drop_24h"),
        "future_up_6h_med": safe_median(first, "future_up_6h"),
        "future_up_24h_med": safe_median(first, "future_up_24h"),
        "short_adverse_24h_med": safe_median(first, "short_adverse_before_down5_24h"),
        "delay_from_entry_h_med": safe_median_series((first["decision_time"] - first["entry_time"]) / 3_600_000),
        "delay_from_peak_h_med": safe_median(first, "delay_from_peak_h"),
        "signals_before_peak": int((first["decision_time"] <= first["peak_time"]).sum()),
        "signals_after_peak": int((first["decision_time"] > first["peak_time"]).sum()),
        "states": {str(k): int(v) for k, v in first["behavior_state"].value_counts().to_dict().items()},
        "families": {str(k): int(v) for k, v in first["family"].value_counts().to_dict().items()},
    }


def peak_frame(rows: pd.DataFrame) -> pd.DataFrame:
    idx = rows.groupby("life_id")["orig_ctx_high_since_entry"].idxmax()
    return rows.loc[idx, ["life_id", "decision_time", "orig_ctx_high_since_entry"]].rename(
        columns={"decision_time": "peak_time", "orig_ctx_high_since_entry": "peak_gain"}
    )


def best_replay(replay: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for name, row in replay.items():
        if not row.get("signals"):
            continue
        score = (
            (row.get("future_drop_24h_med") or 0.0)
            - (row.get("short_adverse_24h_med") or row.get("future_up_24h_med") or 0.0)
            + 0.08 * (row.get("precision") or 0.0)
            + 0.03 * (row.get("signals_before_peak") or 0.0)
        )
        candidates.append((score, name, row))
    if not candidates:
        return {}
    score, name, row = max(candidates, key=lambda x: x[0])
    return {"threshold_name": name, "rank_score": float(score), **row}


def split_summary(data: pd.DataFrame, split: dict[str, np.ndarray]) -> dict[str, Any]:
    out = {}
    for name, idx in split.items():
        part = data.iloc[idx]
        out[name] = {
            "rows": int(len(part)),
            "lifecycles": int(part["life_id"].nunique()) if len(part) else 0,
            "positives": int(part["target"].sum()) if len(part) else 0,
            "positive_rate": float(part["target"].mean()) if len(part) else None,
        }
    return out


def dataset_profile(rows: pd.DataFrame) -> dict[str, Any]:
    peak = rows.groupby("life_id")["orig_ctx_high_since_entry"].max()
    return {
        "family_counts": {str(k): int(v) for k, v in rows[["life_id", "family"]].drop_duplicates()["family"].value_counts().to_dict().items()},
        "row_family_counts": {str(k): int(v) for k, v in rows["family"].value_counts().to_dict().items()},
        "behavior_counts": {str(k): int(v) for k, v in rows["behavior_state"].value_counts().to_dict().items()},
        "median_peak_gain": float(peak.median()) if len(peak) else None,
        "p75_peak_gain": float(peak.quantile(0.75)) if len(peak) else None,
        "median_rows_per_lifecycle": float(rows.groupby("life_id").size().median()) if len(rows) else None,
    }


def maybe_auc(y: pd.Series, score: np.ndarray) -> float | None:
    yv = y.astype(int).to_numpy()
    if len(np.unique(yv)) < 2:
        return None
    return float(roc_auc_score(yv, score))


def maybe_ap(y: pd.Series, score: np.ndarray) -> float | None:
    yv = y.astype(int).to_numpy()
    if len(np.unique(yv)) < 2:
        return None
    return float(average_precision_score(yv, score))


def feature_importance(model: lgb.LGBMClassifier, cols: list[str], n: int = 25) -> list[dict[str, Any]]:
    gain = model.booster_.feature_importance(importance_type="gain")
    order = np.argsort(gain)[::-1][:n]
    return [{"feature": cols[int(i)], "gain": float(gain[int(i)])} for i in order]


def safe_median(rows: pd.DataFrame, col: str) -> float | None:
    if rows.empty or col not in rows:
        return None
    val = pd.to_numeric(rows[col], errors="coerce").median()
    return float(val) if pd.notna(val) and np.isfinite(val) else None


def safe_median_series(series: pd.Series) -> float | None:
    val = pd.to_numeric(series, errors="coerce").median()
    return float(val) if pd.notna(val) and np.isfinite(val) else None


def compact_task_summary(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("skipped"):
        return result
    return {
        "task": result["task"],
        "rows": result["rows"],
        "lifecycles": result["lifecycles"],
        "positive_rate": result["positive_rate"],
        "test_auc": result["test_auc"],
        "best": result["best"],
    }


def render_markdown(results: dict[str, Any]) -> str:
    lines = [
        "# High Pump 40% Experts",
        "",
        "Experiment-only high-pump lifecycle reset from the first 40% crossing.",
        "",
        f"- Dense: `{results['dataset']}`",
        f"- High dataset: `{results['high_dataset']}`",
        f"- Min gain: {results['min_gain'] * 100:.1f}%",
        f"- Rows: {results['rows']}",
        f"- Lifecycles: {results['lifecycles']}",
        f"- Feature count: {results['feature_count']}",
        f"- Profile: `{json.dumps(results['profile'], ensure_ascii=False)}`",
        "",
        "| Task | Label | Rows | Lifecycles | Test AUC | Threshold | Signals | Before Peak | After Peak | Drop6 | Drop24 | Up24 | Adv24 | Delay From Peak |",
        "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for task, result in results["tasks"].items():
        if result.get("skipped"):
            lines.append(f"| {task} | {result.get('label', '')} | {result.get('rows', 0)} | {result.get('lifecycles', 0)} | skipped: {result['skipped']} | - | - | - | - | - | - | - | - | - |")
            continue
        best = result.get("best", {})
        lines.append(
            f"| {task} | {result.get('label', '')} | {result['rows']} | {result['lifecycles']} | {num(result.get('test_auc'))} | "
            f"{best.get('threshold_name', '-')} | {best.get('signals', 0)} | {best.get('signals_before_peak', 0)} | {best.get('signals_after_peak', 0)} | "
            f"{pct(best.get('future_drop_6h_med'))} | {pct(best.get('future_drop_24h_med'))} | {pct(best.get('future_up_24h_med'))} | "
            f"{pct(best.get('short_adverse_24h_med'))} | {num(best.get('delay_from_peak_h_med'))}h |"
        )
    return "\n".join(lines)


def pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f}"


if __name__ == "__main__":
    raise SystemExit(main())
