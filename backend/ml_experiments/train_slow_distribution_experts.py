"""Train slow-distribution-specific high-zone and short-entry experts.

This experiment intentionally does not reuse the fast-dump "exact top then
fast break" label design. Slow distribution is treated as a high-level range:

1. distribution_warning: the high zone is mature enough that chasing/holding
   long is unattractive.
2. breakdown_short: the high range has started to fail with acceptable short
   adverse.

The script is experiment-only and does not modify production signal logic.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_experiments.train_dense_lifecycle_experts import (
    DAY_MS,
    BEHAVIOR_ORDER,
    ENTRY_CONTEXT,
    FEATS,
    feature_importance,
    lifecycle_split,
    maybe_ap,
    maybe_auc,
    safe_median,
    safe_median_series,
)


SLOW_FAMILIES = ["slow_distribution"]
SLOW_PLUS_SECOND = ["slow_distribution", "second_distribution"]
DERIVED_FEATURES = [
    "slow_noise",
    "slow_amp",
    "slow_ret",
    "slow_drawdown",
    "slow_drawdown_over_amp",
    "slow_drawdown_over_noise",
    "slow_hours_log",
    "slow_range_pressure",
    "slow_sell_pressure",
    "slow_ret6_over_noise",
    "slow_dist21_over_noise",
    "slow_maturity",
]
TASKS = ["distribution_warning", "breakdown_short"]


@dataclass(frozen=True)
class SlowRecipe:
    name: str
    min_amp: float
    min_hours: float
    min_drawdown: float
    max_warning_drawdown: float
    drop72_base: float
    drop72_amp: float
    up24_cap: float
    short_drop24_base: float
    short_drop24_amp: float
    short_adv_cap: float
    min_short_drawdown_amp: float
    min_short_drawdown_noise: float
    weak_ret6_noise: float


RECIPES = [
    SlowRecipe("balanced", 0.10, 12.0, 0.025, 0.24, 0.08, 0.08, 0.16, 0.07, 0.07, 0.065, 0.12, 1.40, 1.30),
    SlowRecipe("mature_range", 0.12, 18.0, 0.035, 0.28, 0.09, 0.09, 0.18, 0.075, 0.08, 0.070, 0.14, 1.45, 1.35),
    SlowRecipe("low_adverse", 0.10, 18.0, 0.035, 0.22, 0.08, 0.08, 0.12, 0.065, 0.07, 0.050, 0.13, 1.60, 1.45),
    SlowRecipe("late_break", 0.12, 24.0, 0.045, 0.32, 0.10, 0.07, 0.20, 0.08, 0.07, 0.060, 0.16, 1.60, 1.55),
    SlowRecipe("wide_second", 0.08, 12.0, 0.020, 0.26, 0.07, 0.10, 0.18, 0.06, 0.09, 0.070, 0.12, 1.25, 1.25),
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dense = pd.read_parquet(args.dataset).copy()
    dense = add_slow_features(dense)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "dataset": args.dataset,
        "families": {},
        "recipes": [asdict(r) for r in RECIPES],
        "note": "Experiment-only slow-distribution labels; production is unchanged.",
    }
    family_sets = {
        "slow_only": SLOW_FAMILIES,
        "slow_plus_second": SLOW_PLUS_SECOND,
    }
    for family_set_name, families in family_sets.items():
        rows = dense[dense["family"].isin(families)].copy()
        if rows.empty:
            continue
        fam_result: dict[str, Any] = {
            "families": families,
            "rows": int(len(rows)),
            "lifecycles": int(rows[["symbol", "entry_time"]].drop_duplicates().shape[0]),
            "tasks": {},
        }
        feature_cols = feature_columns(rows)
        for task in TASKS:
            task_runs = []
            for recipe in RECIPES:
                print(f"training {family_set_name} {task} {recipe.name}", flush=True)
                run = train_one(rows, task, recipe, feature_cols, out_dir / family_set_name / task, args)
                task_runs.append(run)
                print(json.dumps(run_summary(run), ensure_ascii=False), flush=True)
            task_runs.sort(key=rank_run, reverse=True)
            fam_result["tasks"][task] = task_runs
        results["families"][family_set_name] = fam_result

    out_json = out_dir / "slow_distribution_experts.json"
    out_md = out_dir / "slow_distribution_experts.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train slow-distribution high-zone and short-entry experts.")
    parser.add_argument("--dataset", default="storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--out-dir", default="storage/ml/slow_distribution_experts")
    parser.add_argument("--min-train-rows", type=int, default=200)
    parser.add_argument("--min-train-positives", type=int, default=30)
    return parser.parse_args(argv)


def add_slow_features(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    noise = slow_noise(out)
    amp = pd.to_numeric(out["ctx_high_since_entry"], errors="coerce").clip(lower=0.0)
    ret = pd.to_numeric(out["ctx_ret_since_entry"], errors="coerce")
    drawdown = (-pd.to_numeric(out["ctx_drawdown_from_entry_high"], errors="coerce")).clip(lower=0.0)
    red = pd.to_numeric(out["ctx_red_bar_share"], errors="coerce").fillna(0.0)
    tsell = pd.to_numeric(out["ctx_taker_sell_mean"], errors="coerce").fillna(0.5)
    qv_recent = pd.to_numeric(out["ctx_qv_recent_ratio"], errors="coerce").fillna(1.0).clip(lower=0.0)
    hours = pd.to_numeric(out["ctx_hours_since_entry"], errors="coerce").fillna(0.0).clip(lower=0.0)
    out["slow_noise"] = noise
    out["slow_amp"] = amp
    out["slow_ret"] = ret
    out["slow_drawdown"] = drawdown
    out["slow_drawdown_over_amp"] = drawdown / np.maximum(amp, 0.03)
    out["slow_drawdown_over_noise"] = drawdown / np.maximum(noise, 0.006)
    out["slow_hours_log"] = np.log1p(hours)
    out["slow_sell_pressure"] = (red - 0.45).clip(lower=0.0) + (tsell - 0.50).clip(lower=0.0)
    out["slow_range_pressure"] = out["slow_sell_pressure"] * np.log1p(qv_recent) * np.sqrt(np.maximum(drawdown, 0.0))
    out["slow_ret6_over_noise"] = pd.to_numeric(out["ret_6"], errors="coerce").fillna(0.0) / np.maximum(noise, 0.006)
    out["slow_dist21_over_noise"] = pd.to_numeric(out["dist_ema21"], errors="coerce").fillna(0.0) / np.maximum(noise, 0.006)
    out["slow_maturity"] = np.log1p(hours) * np.log1p(np.maximum(amp * 10.0, 0.0))
    return out


def slow_noise(rows: pd.DataFrame) -> pd.Series:
    atr = pd.to_numeric(rows["atr_14"], errors="coerce").fillna(0.0)
    retstd = pd.to_numeric(rows["retstd_20"], errors="coerce").fillna(0.0) * 1.5
    return pd.Series(np.clip(np.maximum(atr.to_numpy(), retstd.to_numpy()), 0.006, 0.10), index=rows.index)


def feature_columns(rows: pd.DataFrame) -> list[str]:
    behavior_cols = [f"behavior_{x}" for x in BEHAVIOR_ORDER if f"behavior_{x}" in rows.columns]
    allowed = set(FEATS + ENTRY_CONTEXT + DERIVED_FEATURES + behavior_cols)
    return [c for c in rows.columns if c in allowed and pd.api.types.is_numeric_dtype(rows[c])]


def make_task_frame(rows: pd.DataFrame, task: str, recipe: SlowRecipe) -> pd.DataFrame:
    amp = rows["slow_amp"]
    ret = rows["slow_ret"]
    drawdown = rows["slow_drawdown"]
    noise = rows["slow_noise"]
    hours = pd.to_numeric(rows["ctx_hours_since_entry"], errors="coerce").fillna(0.0)
    weak_ret6 = pd.to_numeric(rows["ret_6"], errors="coerce").fillna(0.0) <= -np.maximum(0.018, recipe.weak_ret6_noise * noise)
    below_ema = pd.to_numeric(rows["dist_ema21"], errors="coerce").fillna(0.0) <= -np.maximum(0.010, 0.80 * noise)
    sell_pressure = rows["slow_sell_pressure"] >= 0.04
    volume_active = pd.to_numeric(rows["ctx_qv_recent_ratio"], errors="coerce").fillna(0.0) >= 1.20

    if task == "distribution_warning":
        drop_target = np.maximum(recipe.drop72_base, recipe.drop72_amp * amp + 1.1 * noise).clip(0.06, 0.24)
        up_cap = np.maximum(recipe.up24_cap, 2.0 * noise).clip(0.08, 0.22)
        eligible = (
            (amp >= recipe.min_amp)
            & (hours >= recipe.min_hours)
            & (drawdown >= recipe.min_drawdown)
            & (drawdown <= recipe.max_warning_drawdown)
            & (ret >= -0.06)
            & (sell_pressure | volume_active | (rows["behavior_state"].isin(["distribution", "climax_risk", "pullback_risk", "trend_hold"])))
        )
        target = (rows["future_drop_72h"] >= drop_target) & (rows["future_up_24h"] <= up_cap)
        label_cols = {
            "label_drop72_target": drop_target,
            "label_up24_cap": up_cap,
        }
    elif task == "breakdown_short":
        min_short_dd = np.maximum(recipe.min_drawdown, recipe.min_short_drawdown_amp * amp + recipe.min_short_drawdown_noise * noise).clip(0.04, 0.16)
        drop_target = np.maximum(recipe.short_drop24_base, recipe.short_drop24_amp * amp + 1.2 * noise).clip(0.055, 0.20)
        adv_cap = np.maximum(recipe.short_adv_cap, 1.6 * noise).clip(0.035, 0.10)
        eligible = (
            (amp >= recipe.min_amp)
            & (hours >= recipe.min_hours)
            & (drawdown >= min_short_dd)
            & ((weak_ret6 | below_ema | sell_pressure) & (volume_active | (drawdown >= min_short_dd * 1.25)))
        )
        target = (
            (rows["future_drop_24h"] >= drop_target)
            & (rows["short_adverse_before_down5_24h"] <= adv_cap)
            & (rows["future_drop_12h"] >= 0.035)
        )
        label_cols = {
            "label_drop24_target": drop_target,
            "label_adv24_cap": adv_cap,
            "label_min_short_drawdown": min_short_dd,
        }
    else:
        raise ValueError(task)
    data = rows[eligible].copy()
    data["target"] = target.loc[data.index].astype("int8")
    for name, values in label_cols.items():
        data[name] = values.loc[data.index]
    return data


def train_one(rows: pd.DataFrame, task: str, recipe: SlowRecipe, feature_cols: list[str], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    data = make_task_frame(rows, task, recipe)
    result_base = {"task": task, "recipe": recipe.name, "recipe_params": asdict(recipe), "rows": int(len(data))}
    if len(data) < args.min_train_rows:
        return {**result_base, "skipped": "rows_too_small"}
    if int(data["target"].sum()) < args.min_train_positives:
        return {**result_base, "skipped": "positives_too_small", "positives": int(data["target"].sum())}
    split = lifecycle_split(data)
    train = data.iloc[split["train"]]
    val = data.iloc[split["val"]]
    test = data.iloc[split["test"]]
    if train.empty or val.empty or test.empty:
        return {**result_base, "skipped": "empty_split", "split": split_summary(data, split)}
    pos = int(train["target"].sum())
    neg = int(len(train) - pos)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=520,
        learning_rate=0.025,
        num_leaves=24,
        min_child_samples=35,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=3.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model.fit(train[feature_cols], train["target"])
    val_score = model.predict_proba(val[feature_cols])[:, 1]
    test_score = model.predict_proba(test[feature_cols])[:, 1]
    thresholds = {f"q{int(q * 100)}": float(np.quantile(val_score, q)) for q in (0.75, 0.80, 0.85, 0.90, 0.95)}
    replay = gated_first_signal_replay(test, test_score, thresholds, task)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / f"{task}_{recipe.name}.txt"
    model.booster_.save_model(str(model_path))
    result = {
        **result_base,
        "positives": int(data["target"].sum()),
        "positive_rate": float(data["target"].mean()),
        "split": split_summary(data, split),
        "test": row_metrics(test, test_score),
        "thresholds_from_val": {k: round(v, 6) for k, v in thresholds.items()},
        "test_first_signal": replay,
        "label_stats": label_stats(data),
        "feature_importance": feature_importance(model, feature_cols),
        "model_path": str(model_path),
    }
    (out_dir / f"{task}_{recipe.name}_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def row_metrics(rows: pd.DataFrame, score: np.ndarray) -> dict[str, Any]:
    y = rows["target"].astype(int).to_numpy()
    out: dict[str, Any] = {
        "rows": int(len(rows)),
        "lifecycles": int(rows[["symbol", "entry_time"]].drop_duplicates().shape[0]),
        "base_rate": float(y.mean()) if len(y) else None,
        "auc": maybe_auc(y, score),
        "ap": maybe_ap(y, score),
    }
    for q in (0.80, 0.90, 0.95):
        threshold = float(np.quantile(score, q)) if len(score) else np.nan
        selected = score >= threshold
        out[f"q{int(q * 100)}"] = {
            "threshold": round(threshold, 6) if np.isfinite(threshold) else None,
            "selected": int(selected.sum()),
            "precision": float(y[selected].mean()) if int(selected.sum()) else None,
        }
    return out


def gated_first_signal_replay(rows: pd.DataFrame, score: np.ndarray, thresholds: dict[str, float], task: str) -> dict[str, Any]:
    if task == "distribution_warning":
        gates = {
            "all": None,
            "distribution_zone": {"distribution", "climax_risk", "trend_hold"},
            "distribution_pullback": {"distribution", "climax_risk", "pullback_risk"},
        }
    else:
        gates = {
            "all": None,
            "distribution_pullback": {"distribution", "climax_risk", "pullback_risk"},
            "breakdown_pullback": {"breakdown", "pullback_risk"},
            "breakdown": {"breakdown"},
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
        out[gate_name] = {name: first_signal(gate_rows, gate_score, threshold, task) for name, threshold in thresholds.items()}
    return out


def first_signal(rows: pd.DataFrame, score: np.ndarray, threshold: float, task: str) -> dict[str, Any]:
    if rows.empty:
        return {"lifecycles": 0, "triggered": 0, "coverage": 0.0}
    replay = rows.copy()
    replay["score"] = score
    total = replay[["symbol", "entry_time"]].drop_duplicates().shape[0]
    triggered = replay[replay["score"] >= threshold].sort_values(["symbol", "entry_time", "decision_time"])
    first = triggered.groupby(["symbol", "entry_time"], as_index=False).first()
    if first.empty:
        return {"threshold": round(float(threshold), 6), "lifecycles": int(total), "triggered": 0, "coverage": 0.0}
    y = first["target"].astype(int).to_numpy()
    payload = {
        "threshold": round(float(threshold), 6),
        "lifecycles": int(total),
        "triggered": int(len(first)),
        "coverage": float(len(first) / total) if total else None,
        "precision": float(y.mean()) if len(y) else None,
        "median_delay_hours": safe_median_series((first["decision_time"] - first["entry_time"]) / 3_600_000),
        "median_future_up_24h": safe_median(first, "future_up_24h"),
        "median_future_drop_6h": safe_median(first, "future_drop_6h"),
        "median_future_drop_12h": safe_median(first, "future_drop_12h"),
        "median_future_drop_24h": safe_median(first, "future_drop_24h"),
        "median_future_drop_72h": safe_median(first, "future_drop_72h"),
        "behavior_mix": {str(k): int(v) for k, v in first["behavior_state"].value_counts().to_dict().items()},
    }
    if task == "breakdown_short":
        payload["median_short_adverse_6h"] = safe_median(first, "short_adverse_before_down5_6h")
        payload["median_short_adverse_24h"] = safe_median(first, "short_adverse_before_down5_24h")
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


def label_stats(data: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in [c for c in data.columns if c.startswith("label_")]:
        s = pd.to_numeric(data[col], errors="coerce").dropna()
        if s.empty:
            continue
        out[col] = {
            "median": float(s.quantile(0.5)),
            "p75": float(s.quantile(0.75)),
            "p90": float(s.quantile(0.90)),
        }
    return out


def rank_run(run: dict[str, Any]) -> float:
    if run.get("skipped"):
        return -1e9
    task = run["task"]
    if task == "distribution_warning":
        candidates = [
            run["test_first_signal"].get("distribution_zone", {}).get("q80", {}),
            run["test_first_signal"].get("distribution_pullback", {}).get("q80", {}),
            run["test_first_signal"].get("all", {}).get("q80", {}),
        ]
        best = max(candidates, key=warning_score)
        return warning_score(best)
    candidates = [
        run["test_first_signal"].get("distribution_pullback", {}).get("q90", {}),
        run["test_first_signal"].get("breakdown_pullback", {}).get("q90", {}),
        run["test_first_signal"].get("all", {}).get("q90", {}),
    ]
    best = max(candidates, key=short_score)
    return short_score(best)


def warning_score(replay: dict[str, Any]) -> float:
    if not replay or not replay.get("triggered"):
        return -1e9
    return (
        1.8 * (replay.get("precision") or 0.0)
        + 0.8 * (replay.get("coverage") or 0.0)
        + 1.5 * (replay.get("median_future_drop_72h") or 0.0)
        - 1.0 * (replay.get("median_future_up_24h") or 0.0)
    )


def short_score(replay: dict[str, Any]) -> float:
    if not replay or not replay.get("triggered"):
        return -1e9
    return (
        1.8 * (replay.get("precision") or 0.0)
        + 0.8 * (replay.get("coverage") or 0.0)
        + 2.0 * (replay.get("median_future_drop_24h") or 0.0)
        - 2.5 * (replay.get("median_short_adverse_24h") or 0.0)
    )


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("skipped"):
        return run
    task = run["task"]
    if task == "distribution_warning":
        replay = run["test_first_signal"].get("distribution_zone", {}).get("q80", {})
    else:
        replay = run["test_first_signal"].get("distribution_pullback", {}).get("q90", {})
    return {
        "task": task,
        "recipe": run["recipe"],
        "rows": run["rows"],
        "positive_rate": run.get("positive_rate"),
        "auc": run["test"].get("auc"),
        "replay": replay,
        "rank": rank_run(run),
    }


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Slow Distribution Experts",
        "",
        "Slow-distribution-specific high-zone and short-entry experiments.",
        "",
        f"- Dataset: `{results['dataset']}`",
        "",
        "## Best Runs",
        "",
        "| Family Set | Task | Recipe | Rows | Base | AUC | Gate/Threshold | Coverage | Precision | Up24 | Drop24 | Drop72 | Short Adv24 |",
        "|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for family_set, fam_result in results["families"].items():
        for task, runs in fam_result["tasks"].items():
            best_run = next((r for r in runs if not r.get("skipped")), None)
            if best_run is None:
                lines.append(f"| {family_set} | {task} | skipped | - | - | - | - | - | - | - | - | - | - |")
                continue
            gate, threshold, replay = best_replay(best_run)
            lines.append(
                f"| {family_set} | {task} | {best_run['recipe']} | {best_run['rows']} | {pct(best_run.get('positive_rate'))} | "
                f"{num(best_run['test'].get('auc'))} | {gate}/{threshold} | {pct(replay.get('coverage'))} | {pct(replay.get('precision'))} | "
                f"{pct(replay.get('median_future_up_24h'))} | {pct(replay.get('median_future_drop_24h'))} | "
                f"{pct(replay.get('median_future_drop_72h'))} | {pct(replay.get('median_short_adverse_24h'))} |"
            )
    lines += [
        "",
        "## All Runs",
        "",
        "| Family Set | Task | Recipe | Rank | Rows | AUC | Best Gate | Coverage | Precision | Up24 | Drop24 | Drop72 | Short Adv24 |",
        "|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for family_set, fam_result in results["families"].items():
        for task, runs in fam_result["tasks"].items():
            for run in runs:
                if run.get("skipped"):
                    lines.append(f"| {family_set} | {task} | {run['recipe']} | - | {run.get('rows', 0)} | skipped: {run['skipped']} | - | - | - | - | - | - | - |")
                    continue
                gate, threshold, replay = best_replay(run)
                lines.append(
                    f"| {family_set} | {task} | {run['recipe']} | {num(rank_run(run))} | {run['rows']} | {num(run['test'].get('auc'))} | "
                    f"{gate}/{threshold} | {pct(replay.get('coverage'))} | {pct(replay.get('precision'))} | "
                    f"{pct(replay.get('median_future_up_24h'))} | {pct(replay.get('median_future_drop_24h'))} | "
                    f"{pct(replay.get('median_future_drop_72h'))} | {pct(replay.get('median_short_adverse_24h'))} |"
                )
    return "\n".join(lines)


def best_replay(run: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    task = run["task"]
    options: list[tuple[str, str, dict[str, Any], float]] = []
    for gate, gate_result in run["test_first_signal"].items():
        for threshold in ("q75", "q80", "q85", "q90", "q95"):
            replay = gate_result.get(threshold, {})
            score = warning_score(replay) if task == "distribution_warning" else short_score(replay)
            options.append((gate, threshold, replay, score))
    if not options:
        return "-", "-", {}
    gate, threshold, replay, _score = max(options, key=lambda x: x[3])
    return gate, threshold, replay


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
