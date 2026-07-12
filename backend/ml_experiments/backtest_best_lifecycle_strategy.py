"""Backtest the current best lifecycle strategy on dense 15m replay data.

This is an offline research backtest, not production trading code. It combines:

- long entry ML from long_entries.parquet;
- dyn_big_pump_tolerant behavior router;
- fast_dump dynamic top/short experts;
- slow_distribution defensive warning and slow+second late-break short experts.

All model thresholds are derived from the validation slice. The reported
strategy metrics are computed only on the final test slice.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_experiments.optimize_dynamic_router_thresholds import CONFIGS, assign_dynamic_states
from ml_experiments.train_dense_lifecycle_experts import (
    DAY_MS,
    dynamic_task_labels,
    feature_columns as dense_feature_columns,
    safe_median,
    safe_median_series,
)
from ml_experiments.train_lifecycle_models import LONG_FEATURES
from ml_experiments.train_slow_distribution_experts import (
    RECIPES,
    add_slow_features,
    feature_columns as slow_feature_columns,
    make_task_frame as make_slow_task_frame,
)


FAST_GATE = {"distribution", "climax_risk", "pullback_risk"}
SLOW_GATE = {"distribution", "climax_risk", "pullback_risk"}


@dataclass(frozen=True)
class SplitTimes:
    q70: float
    q85: float
    embargo: int = 3 * DAY_MS


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dense = pd.read_parquet(args.dense).copy()
    entries = pd.read_parquet(args.entries).copy()
    split = split_times(dense)

    router_cfg = next(cfg for cfg in CONFIGS if cfg.name == args.router)
    dense["behavior_state"] = assign_dynamic_states(dense, router_cfg)
    for behavior in sorted(dense["behavior_state"].dropna().unique()):
        dense[f"behavior_{behavior}"] = (dense["behavior_state"] == behavior).astype("int8")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    long_result = train_long_models(entries, split)
    expert_quantiles = {
        "fast_top": args.fast_top_q,
        "fast_short": args.fast_short_q,
        "slow_warning": args.slow_warning_q,
        "slow_short": args.slow_short_q,
    }
    expert_result = train_experts(dense, split, out_dir, expert_quantiles)
    signals = build_signals(dense, expert_result, split, args.multi_cooldown_bars, args.bar_minutes)
    strategy = evaluate_strategy(dense, entries, long_result, signals, split)

    results = {
        "dense": args.dense,
        "entries": args.entries,
        "router": args.router,
        "multi_cooldown_bars": args.multi_cooldown_bars,
        "bar_minutes": args.bar_minutes,
        "expert_quantiles": expert_quantiles,
        "split": {
            "train_until": int(split.q70),
            "val_start": int(split.q70 + split.embargo),
            "val_until": int(split.q85),
            "test_start": int(split.q85 + split.embargo),
        },
        "long_models": long_result["summary"],
        "experts": expert_result["summary"],
        "signals": signals["summary"],
        "multi_signals": signals["multi_summary"],
        "strategy": strategy,
        "notes": [
            "Offline research backtest only; production logic is unchanged.",
            "Signals use closed 15m dense replay rows and validation-derived thresholds.",
            "Long-chain metrics are reported on target lifecycle rows where dense replay exists.",
        ],
    }
    out_json = out_dir / "best_lifecycle_backtest.json"
    out_md = out_dir / "best_lifecycle_backtest.md"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(results), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest current best lifecycle strategy.")
    parser.add_argument("--dense", default="storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--entries", default="storage/ml/lifecycle/long_entries.parquet")
    parser.add_argument("--router", default="dyn_big_pump_tolerant")
    parser.add_argument("--out-dir", default="storage/ml/best_lifecycle_backtest")
    parser.add_argument("--multi-cooldown-bars", type=int, default=4, help="15m bars between repeated same-signal triggers.")
    parser.add_argument("--bar-minutes", type=int, default=15)
    parser.add_argument("--fast-top-q", type=float, default=0.80)
    parser.add_argument("--fast-short-q", type=float, default=0.90)
    parser.add_argument("--slow-warning-q", type=float, default=0.95)
    parser.add_argument("--slow-short-q", type=float, default=0.90)
    return parser.parse_args(argv)


def split_times(dense: pd.DataFrame) -> SplitTimes:
    unique_times = np.sort(dense["entry_time"].dropna().unique())
    q70, q85 = np.quantile(unique_times, [0.70, 0.85])
    return SplitTimes(float(q70), float(q85))


def split_masks(rows: pd.DataFrame, split: SplitTimes) -> dict[str, pd.Series]:
    t = pd.to_numeric(rows["entry_time"], errors="coerce")
    return {
        "train": t <= split.q70,
        "val": (t >= split.q70 + split.embargo) & (t <= split.q85),
        "test": t >= split.q85 + split.embargo,
    }


def train_long_models(entries: pd.DataFrame, split: SplitTimes) -> dict[str, Any]:
    rows = entries.dropna(subset=LONG_FEATURES + ["y_pump_event", "y_long_start"]).copy()
    masks = split_masks(rows, split)
    pump = fit_binary(rows, "y_pump_event", LONG_FEATURES, masks)
    quality = fit_binary(rows, "y_long_start", LONG_FEATURES, masks)
    rows["pump_score"] = pump["score_all"]
    rows["quality_score"] = quality["score_all"]
    rows["long_score"] = 0.65 * rows["pump_score"] + 0.35 * rows["quality_score"]
    val_scores = rows.loc[masks["val"], "long_score"].to_numpy(float)
    thresholds = {f"q{int(q * 100)}": float(np.quantile(val_scores, q)) for q in (0.80, 0.90, 0.95)}
    test = rows[masks["test"]].copy()
    summary = {
        "rows": int(len(rows)),
        "train_rows": int(masks["train"].sum()),
        "val_rows": int(masks["val"].sum()),
        "test_rows": int(masks["test"].sum()),
        "thresholds_from_val": {k: round(v, 6) for k, v in thresholds.items()},
        "test": {},
    }
    for name, threshold in thresholds.items():
        selected = test[test["long_score"] >= threshold]
        summary["test"][name] = long_entry_metrics(selected)
    key_cols = ["symbol", "entry_time", "entry_price", "pump_score", "quality_score", "long_score"]
    return {
        "summary": summary,
        "scores": rows[key_cols].copy(),
        "thresholds": thresholds,
    }


def fit_binary(rows: pd.DataFrame, target: str, features: list[str], masks: dict[str, pd.Series]) -> dict[str, Any]:
    train = rows[masks["train"]]
    pos = int(train[target].sum())
    neg = int(len(train) - pos)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=420,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=35,
        subsample=0.85,
        colsample_bytree=0.80,
        reg_lambda=2.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model.fit(train[features], train[target])
    return {"model": model, "score_all": model.predict_proba(rows[features])[:, 1]}


def long_entry_metrics(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {"signals": 0}
    return {
        "signals": int(len(rows)),
        "pump_event_rate": mean_or_none(rows, "y_pump_event"),
        "long_start_rate": mean_or_none(rows, "y_long_start"),
        "median_future_high_48h": safe_median(rows, "future_high_48h"),
        "median_adverse_before_up5": safe_median(rows, "adverse_before_up5"),
        "family_mix": {str(k): int(v) for k, v in rows["family"].fillna("none").value_counts().to_dict().items()},
    }


def train_experts(dense: pd.DataFrame, split: SplitTimes, out_dir: Path, quantiles: dict[str, float]) -> dict[str, Any]:
    dense_features = dense_feature_columns(dense)
    fast = dense[dense["family"] == "fast_dump"].copy()
    slow = add_slow_features(dense[dense["family"] == "slow_distribution"].copy())
    slow_plus_second = add_slow_features(dense[dense["family"].isin(["slow_distribution", "second_distribution"])].copy())
    slow_warning_recipe = next(r for r in RECIPES if r.name == "low_adverse")
    slow_short_recipe = next(r for r in RECIPES if r.name == "late_break")

    tasks = {
        "fast_top": make_frame(fast, *dynamic_task_labels(fast, "fast_dump", "top_exit")),
        "fast_short": make_frame(fast, *dynamic_task_labels(fast, "fast_dump", "short_clean")),
        "slow_warning": make_slow_task_frame(slow, "distribution_warning", slow_warning_recipe),
        "slow_short": make_slow_task_frame(slow_plus_second, "breakdown_short", slow_short_recipe),
    }
    feature_sets = {
        "fast_top": dense_features,
        "fast_short": dense_features,
        "slow_warning": slow_feature_columns(slow),
        "slow_short": slow_feature_columns(slow_plus_second),
    }
    trained: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    for name, frame in tasks.items():
        model_result = train_expert_frame(frame, feature_sets[name], split, quantiles[name])
        trained[name] = model_result
        model_path = out_dir / "models" / f"{name}.txt"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_result["model"].booster_.save_model(str(model_path))
        summary[name] = {
            "rows": int(len(frame)),
            "positive_rate": float(frame["target"].mean()) if len(frame) else None,
            "train_rows": int(model_result["masks"]["train"].sum()),
            "val_rows": int(model_result["masks"]["val"].sum()),
            "test_rows": int(model_result["masks"]["test"].sum()),
            "threshold_quantile": quantiles[name],
            "threshold": round(float(model_result["threshold"]), 6),
            "test_row_precision": threshold_precision(frame[model_result["masks"]["test"]], model_result["score_all"][model_result["masks"]["test"].to_numpy()], model_result["threshold"]),
            "model_path": str(model_path),
        }
    return {"models": trained, "frames": tasks, "summary": summary}


def make_frame(rows: pd.DataFrame, eligible: pd.Series, target: pd.Series, label_cols: dict[str, pd.Series]) -> pd.DataFrame:
    out = rows[eligible].copy()
    out["target"] = target.loc[out.index].astype("int8")
    for name, values in label_cols.items():
        out[name] = values.loc[out.index]
    return out


def train_expert_frame(frame: pd.DataFrame, features: list[str], split: SplitTimes, quantile: float) -> dict[str, Any]:
    masks = split_masks(frame, split)
    train = frame[masks["train"]]
    val = frame[masks["val"]]
    if train.empty or val.empty:
        raise SystemExit("empty train/val split for expert")
    pos = int(train["target"].sum())
    neg = int(len(train) - pos)
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=520,
        learning_rate=0.025,
        num_leaves=28,
        min_child_samples=30,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=3.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model.fit(train[features], train["target"])
    score_all = model.predict_proba(frame[features])[:, 1]
    threshold = float(np.quantile(score_all[masks["val"].to_numpy()], quantile))
    return {"model": model, "score_all": score_all, "threshold": threshold, "masks": masks, "features": features}


def threshold_precision(rows: pd.DataFrame, scores: np.ndarray, threshold: float) -> dict[str, Any]:
    selected = scores >= threshold
    if not int(selected.sum()):
        return {"selected": 0, "precision": None}
    y = rows["target"].astype(int).to_numpy()
    return {"selected": int(selected.sum()), "precision": float(y[selected].mean())}


def build_signals(dense: pd.DataFrame, expert_result: dict[str, Any], split: SplitTimes, cooldown_bars: int, bar_minutes: int) -> dict[str, Any]:
    frames = expert_result["frames"]
    models = expert_result["models"]
    signal_frames = {}
    multi_frames = {}
    signal_frames["fast_top"] = first_signal(
        frames["fast_top"],
        models["fast_top"],
        split,
        FAST_GATE,
        "top",
    )
    signal_frames["fast_short"] = first_signal(
        frames["fast_short"],
        models["fast_short"],
        split,
        FAST_GATE,
        "short",
    )
    signal_frames["slow_warning"] = first_signal(
        frames["slow_warning"],
        models["slow_warning"],
        split,
        SLOW_GATE,
        "top",
    )
    signal_frames["slow_short"] = first_signal(
        frames["slow_short"],
        models["slow_short"],
        split,
        SLOW_GATE,
        "short",
    )
    multi_frames["fast_top"] = multi_signal(frames["fast_top"], models["fast_top"], split, FAST_GATE, "top", cooldown_bars, bar_minutes)
    multi_frames["fast_short"] = multi_signal(frames["fast_short"], models["fast_short"], split, FAST_GATE, "short", cooldown_bars, bar_minutes)
    multi_frames["slow_warning"] = multi_signal(frames["slow_warning"], models["slow_warning"], split, SLOW_GATE, "top", cooldown_bars, bar_minutes)
    multi_frames["slow_short"] = multi_signal(frames["slow_short"], models["slow_short"], split, SLOW_GATE, "short", cooldown_bars, bar_minutes)
    summary = {name: signal_metrics(frame, kind=("short" if "short" in name else "top")) for name, frame in signal_frames.items()}
    multi_summary = {name: signal_metrics(frame, kind=("short" if "short" in name else "top")) for name, frame in multi_frames.items()}
    return {"frames": signal_frames, "multi_frames": multi_frames, "summary": summary, "multi_summary": multi_summary}


def first_signal(frame: pd.DataFrame, model_result: dict[str, Any], split: SplitTimes, gate_states: set[str], kind: str) -> pd.DataFrame:
    test_mask = split_masks(frame, split)["test"].to_numpy()
    scores = model_result["score_all"]
    selected = frame[
        test_mask
        & frame["behavior_state"].isin(gate_states).to_numpy()
        & (scores >= model_result["threshold"])
    ].copy()
    if selected.empty:
        return selected
    selected["score"] = scores[selected.index.map(frame.index.get_loc)]
    selected["signal_kind"] = kind
    return selected.sort_values(["symbol", "entry_time", "decision_time"]).groupby(["symbol", "entry_time"], as_index=False).first()


def multi_signal(frame: pd.DataFrame, model_result: dict[str, Any], split: SplitTimes, gate_states: set[str], kind: str, cooldown_bars: int, bar_minutes: int) -> pd.DataFrame:
    test_mask = split_masks(frame, split)["test"].to_numpy()
    scores = model_result["score_all"]
    selected = frame[
        test_mask
        & frame["behavior_state"].isin(gate_states).to_numpy()
        & (scores >= model_result["threshold"])
    ].copy()
    if selected.empty:
        return selected
    selected["score"] = scores[selected.index.map(frame.index.get_loc)]
    selected["signal_kind"] = kind
    cooldown_ms = max(1, int(cooldown_bars)) * max(1, int(bar_minutes)) * 60_000
    kept = []
    for (_symbol, _entry_time), grp in selected.sort_values(["symbol", "entry_time", "decision_time"]).groupby(["symbol", "entry_time"]):
        last_time: int | None = None
        ordinal = 0
        for row in grp.itertuples(index=False):
            decision_time = int(row.decision_time)
            if last_time is None or decision_time - last_time >= cooldown_ms:
                record = row._asdict()
                ordinal += 1
                record["signal_ordinal"] = ordinal
                kept.append(record)
                last_time = decision_time
    return pd.DataFrame(kept)


def signal_metrics(rows: pd.DataFrame, kind: str) -> dict[str, Any]:
    if rows.empty:
        return {"signals": 0}
    out = {
        "signals": int(len(rows)),
        "family_mix": {str(k): int(v) for k, v in rows["family"].value_counts().to_dict().items()},
        "median_delay_hours": safe_median_series((rows["decision_time"] - rows["entry_time"]) / 3_600_000),
        "median_future_up_24h": safe_median(rows, "future_up_24h"),
        "median_future_drop_6h": safe_median(rows, "future_drop_6h"),
        "median_future_drop_24h": safe_median(rows, "future_drop_24h"),
        "median_future_drop_72h": safe_median(rows, "future_drop_72h"),
        "drop24_ge_8_rate": rate(rows["future_drop_24h"] >= 0.08),
        "drop72_ge_15_rate": rate(rows["future_drop_72h"] >= 0.15),
    }
    if kind == "short":
        out["median_short_adverse_6h"] = safe_median(rows, "short_adverse_before_down5_6h")
        out["median_short_adverse_24h"] = safe_median(rows, "short_adverse_before_down5_24h")
        out["clean_big_short_rate"] = rate((rows["future_drop_24h"] >= 0.08) & (rows["short_adverse_before_down5_24h"] <= 0.05))
    return out


def evaluate_strategy(dense: pd.DataFrame, entries: pd.DataFrame, long_result: dict[str, Any], signals: dict[str, Any], split: SplitTimes) -> dict[str, Any]:
    test_lifecycles = dense[split_masks(dense, split)["test"]][["symbol", "entry_time"]].drop_duplicates().copy()
    long_scores = long_result["scores"]
    life_scores = test_lifecycles.merge(long_scores, on=["symbol", "entry_time"], how="left")
    signal_union = make_signal_union(signals["frames"])
    out: dict[str, Any] = {
        "test_target_lifecycles": int(len(test_lifecycles)),
        "pumpwatch_short_all": signal_metrics(
            pd.concat([signals["frames"]["fast_short"], signals["frames"]["slow_short"]], ignore_index=True),
            "short",
        ),
        "pumpwatch_short_multi": signal_metrics(
            pd.concat([signals["multi_frames"]["fast_short"], signals["multi_frames"]["slow_short"]], ignore_index=True),
            "short",
        ),
        "long_chains": {},
    }
    for name, threshold in long_result["thresholds"].items():
        selected_life = life_scores[life_scores["long_score"] >= threshold].copy()
        out["long_chains"][name] = evaluate_long_chain(selected_life, dense, signal_union)
    return out


def make_signal_union(signal_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for name in ("fast_top", "slow_warning", "fast_short", "slow_short"):
        frame = signal_frames[name].copy()
        if frame.empty:
            continue
        frame["signal_name"] = name
        frame["is_short_signal"] = name.endswith("short")
        parts.append(frame)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def evaluate_long_chain(lifecycles: pd.DataFrame, dense: pd.DataFrame, signal_union: pd.DataFrame) -> dict[str, Any]:
    if lifecycles.empty:
        return {"long_signals_in_target_lifecycles": 0}
    rows = []
    for life in lifecycles.itertuples(index=False):
        symbol = str(life.symbol)
        entry_time = int(life.entry_time)
        life_rows = dense[(dense["symbol"] == symbol) & (dense["entry_time"] == entry_time)].sort_values("decision_time")
        if life_rows.empty:
            continue
        life_signals = signal_union[(signal_union["symbol"] == symbol) & (signal_union["entry_time"] == entry_time)].sort_values("decision_time")
        if life_signals.empty:
            exit_row = life_rows.iloc[-1]
            exit_reason = "timeout_72h"
            short_row = None
        else:
            first_sig = life_signals.iloc[0]
            match = life_rows[life_rows["decision_time"] == int(first_sig.decision_time)]
            exit_row = match.iloc[0] if not match.empty else life_rows[life_rows["decision_time"] <= int(first_sig.decision_time)].iloc[-1]
            exit_reason = str(first_sig.signal_name)
            short_candidates = life_signals[life_signals["is_short_signal"]]
            short_row = short_candidates.iloc[0] if not short_candidates.empty else None
        entry_price = float(life_rows.iloc[0]["entry_price"])
        exit_price = float(exit_row["current_price"])
        rows.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "family": str(life_rows.iloc[0]["family"]),
                "exit_reason": exit_reason,
                "long_return": exit_price / entry_price - 1.0,
                "long_max_up_to_exit": float(exit_row["ctx_high_since_entry"]),
                "long_adverse_to_exit": max(0.0, -float(exit_row["ctx_low_since_entry"])),
                "exit_delay_hours": (int(exit_row["decision_time"]) - entry_time) / 3_600_000,
                "has_short_signal": short_row is not None,
                "short_future_drop_24h": float(short_row["future_drop_24h"]) if short_row is not None else np.nan,
                "short_future_drop_72h": float(short_row["future_drop_72h"]) if short_row is not None else np.nan,
                "short_adverse_24h": float(short_row["short_adverse_before_down5_24h"]) if short_row is not None else np.nan,
            }
        )
    result = pd.DataFrame(rows)
    if result.empty:
        return {"long_signals_in_target_lifecycles": 0}
    return {
        "long_signals_in_target_lifecycles": int(len(result)),
        "family_mix": {str(k): int(v) for k, v in result["family"].value_counts().to_dict().items()},
        "exit_reason_mix": {str(k): int(v) for k, v in result["exit_reason"].value_counts().to_dict().items()},
        "median_long_return": median(result["long_return"]),
        "median_long_max_up_to_exit": median(result["long_max_up_to_exit"]),
        "median_long_adverse_to_exit": median(result["long_adverse_to_exit"]),
        "long_return_positive_rate": rate(result["long_return"] > 0),
        "long_max_up_ge_15_rate": rate(result["long_max_up_to_exit"] >= 0.15),
        "short_signal_rate_after_long": rate(result["has_short_signal"]),
        "median_short_drop24_after_long": median(result["short_future_drop_24h"]),
        "median_short_drop72_after_long": median(result["short_future_drop_72h"]),
        "median_short_adverse24_after_long": median(result["short_adverse_24h"]),
    }


def rate(mask: Any) -> float | None:
    arr = np.asarray(mask)
    if arr.size == 0:
        return None
    return float(np.nanmean(arr.astype(float)))


def mean_or_none(rows: pd.DataFrame, col: str) -> float | None:
    if rows.empty or col not in rows:
        return None
    return float(pd.to_numeric(rows[col], errors="coerce").mean())


def median(values: Any) -> float | None:
    s = pd.to_numeric(values, errors="coerce")
    if len(s) == 0:
        return None
    value = float(np.nanmedian(s))
    return value if np.isfinite(value) else None


def render_report(results: dict[str, Any]) -> str:
    lines = [
        "# Best Lifecycle Strategy Backtest",
        "",
        "Offline OOS backtest for the current best research strategy.",
        "",
        f"- Dense: `{results['dense']}`",
        f"- Entries: `{results['entries']}`",
        f"- Router: `{results['router']}`",
        f"- Multi-signal cooldown: {results.get('multi_cooldown_bars', 4)} x {results.get('bar_minutes', 15)}m bars",
        "",
        "## Long Entry Models",
        "",
        "| Threshold | Signals | Pump Rate | Long Start Rate | Med Future High 48h | Med Adverse Before +5 | Family Mix |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for threshold, stats in results["long_models"]["test"].items():
        lines.append(
            f"| {threshold} | {stats.get('signals', 0)} | {pct(stats.get('pump_event_rate'))} | {pct(stats.get('long_start_rate'))} | "
            f"{pct(stats.get('median_future_high_48h'))} | {pct(stats.get('median_adverse_before_up5'))} | "
            f"`{json.dumps(stats.get('family_mix', {}), ensure_ascii=False)}` |"
        )
    lines += [
        "",
        "## Expert Signals On Test Lifecycles First Only",
        "",
        "| Signal | Count | Delay h | Up24 | Drop6 | Drop24 | Drop72 | Short Adv24 | Clean Big Short | Family Mix |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, stats in results["signals"].items():
        lines.append(
            f"| {name} | {stats.get('signals', 0)} | {num(stats.get('median_delay_hours'))} | {pct(stats.get('median_future_up_24h'))} | "
            f"{pct(stats.get('median_future_drop_6h'))} | {pct(stats.get('median_future_drop_24h'))} | {pct(stats.get('median_future_drop_72h'))} | "
            f"{pct(stats.get('median_short_adverse_24h'))} | {pct(stats.get('clean_big_short_rate'))} | "
            f"`{json.dumps(stats.get('family_mix', {}), ensure_ascii=False)}` |"
        )
    lines += [
        "",
        "## Expert Signals On Test Lifecycles Multi Signal",
        "",
        "| Signal | Count | Delay h | Up24 | Drop6 | Drop24 | Drop72 | Short Adv24 | Clean Big Short | Family Mix |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, stats in results["multi_signals"].items():
        lines.append(
            f"| {name} | {stats.get('signals', 0)} | {num(stats.get('median_delay_hours'))} | {pct(stats.get('median_future_up_24h'))} | "
            f"{pct(stats.get('median_future_drop_6h'))} | {pct(stats.get('median_future_drop_24h'))} | {pct(stats.get('median_future_drop_72h'))} | "
            f"{pct(stats.get('median_short_adverse_24h'))} | {pct(stats.get('clean_big_short_rate'))} | "
            f"`{json.dumps(stats.get('family_mix', {}), ensure_ascii=False)}` |"
        )
    lines += [
        "",
        "## Strategy Chain",
        "",
        f"- Test target lifecycles: {results['strategy']['test_target_lifecycles']}",
        "",
        "| Long Threshold | Target Longs | Med Long Return | Med Max Up | Med Long Adverse | Positive Rate | MaxUp>=15% | Short After Long | Med Short Drop24 | Med Short Adv24 | Exit Mix |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for threshold, stats in results["strategy"]["long_chains"].items():
        lines.append(
            f"| {threshold} | {stats.get('long_signals_in_target_lifecycles', 0)} | {pct(stats.get('median_long_return'))} | "
            f"{pct(stats.get('median_long_max_up_to_exit'))} | {pct(stats.get('median_long_adverse_to_exit'))} | "
            f"{pct(stats.get('long_return_positive_rate'))} | {pct(stats.get('long_max_up_ge_15_rate'))} | "
            f"{pct(stats.get('short_signal_rate_after_long'))} | {pct(stats.get('median_short_drop24_after_long'))} | "
            f"{pct(stats.get('median_short_adverse24_after_long'))} | "
            f"`{json.dumps(stats.get('exit_reason_mix', {}), ensure_ascii=False)}` |"
        )
    short_all = results["strategy"]["pumpwatch_short_all"]
    short_multi = results["strategy"].get("pumpwatch_short_multi", {})
    lines += [
        "",
        "## PumpWatch Short",
        "",
        f"- First-only signals: {short_all.get('signals', 0)}",
        f"- First-only median 24h drop: {pct(short_all.get('median_future_drop_24h'))}",
        f"- First-only median 72h drop: {pct(short_all.get('median_future_drop_72h'))}",
        f"- First-only median 24h adverse: {pct(short_all.get('median_short_adverse_24h'))}",
        f"- First-only clean big short rate: {pct(short_all.get('clean_big_short_rate'))}",
        f"- Multi signals: {short_multi.get('signals', 0)}",
        f"- Multi median 24h drop: {pct(short_multi.get('median_future_drop_24h'))}",
        f"- Multi median 72h drop: {pct(short_multi.get('median_future_drop_72h'))}",
        f"- Multi median 24h adverse: {pct(short_multi.get('median_short_adverse_24h'))}",
        f"- Multi clean big short rate: {pct(short_multi.get('clean_big_short_rate'))}",
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
