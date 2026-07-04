"""Optimize PumpWatch admission rules for the lifecycle strategy.

The production signal engine has two separate gates:

1. discovery admission: whether a symbol enters PumpWatch;
2. signal maturity: whether an active PumpWatch is allowed to emit top/short.

This experiment tests alternative admission rules on closed 15m dense lifecycle
rows. It replays the current production lifecycle models after the simulated
admission time and reports validation/test signal quality.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from ml_experiments.backtest_lifecycle_router_replay import (
    add_slow_features_vectorized,
    combine_signal_streams,
    first_per_lifecycle,
    prepare_dense,
    replay_router_strategy,
    score_all,
    score_high_pump,
    signal_row,
    split_times,
    summarize_signals,
)
from pump_dump_hunter.ml import lifecycle as life
from pump_dump_hunter.ml.model import MLScorer


HORIZONS = ("6h", "24h", "72h")


@dataclass(frozen=True)
class AdmissionRule:
    name: str
    kind: str
    params: dict[str, float | None]

    def mask(self, rows: pd.DataFrame) -> pd.Series:
        if self.kind == "ctx_high":
            return rows["ctx_high_since_entry"].fillna(-1.0) >= float(self.params["min_high"])
        if self.kind == "window":
            return window_mask(rows, self.params)
        if self.kind == "combo":
            return (rows["ctx_high_since_entry"].fillna(-1.0) >= float(self.params["min_high"])) | window_mask(rows, self.params)
        raise ValueError(f"unknown rule kind: {self.kind}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = resolve_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dense = prepare_dense(pd.read_parquet(resolve_path(args.dense)).copy())
    split = split_times(dense)
    scorer = MLScorer(resolve_path(args.models_dir))
    if not scorer.lifecycle_ready or not scorer.lifecycle_router_ready:
        raise RuntimeError(f"lifecycle models not ready: {scorer.error}")

    high = load_high_pump(resolve_path(args.high_pump_dense), dense)
    rules = build_rules()
    if args.rules:
        wanted = {item.strip() for item in args.rules.split(",") if item.strip()}
        rules = [rule for rule in rules if rule.name in wanted]
        missing = sorted(wanted - {rule.name for rule in rules})
        if missing:
            raise SystemExit(f"unknown rules: {missing}")
    replay_args = SimpleNamespace(
        confirm_bars=args.confirm_bars,
        cooldown_bars=args.cooldown_bars,
        margin=args.margin,
        dynamic_thresholds=args.dynamic_thresholds,
        fast_trend_threshold=args.fast_trend_threshold,
        slow_trend_threshold=args.slow_trend_threshold,
        fast_break_threshold=args.fast_break_threshold,
        slow_break_threshold=args.slow_break_threshold,
        slow_mature_threshold=args.slow_mature_threshold,
    )

    replay_partitions = {
        "val": dense[(dense["entry_time"] >= split["val_start"]) & (dense["entry_time"] <= split["val_until"])].copy(),
        "test": dense[dense["entry_time"] >= split["test_start"]].copy(),
    }
    scores_by_part = {name: score_all(rows, scorer) for name, rows in replay_partitions.items()}
    baseline_signals_by_part: dict[str, pd.DataFrame] = {}
    if not args.exact_replay:
        for part_name, part_rows in replay_partitions.items():
            baseline_admissions = first_row_times(part_rows)
            baseline_signals_by_part[part_name] = replay_for_admissions(
                part_rows,
                scores_by_part[part_name],
                high,
                scorer,
                baseline_admissions,
                replay_args,
            )
    admission_only_partitions = {"all_admission": dense.copy()}

    rows: list[dict[str, Any]] = []
    detailed: dict[str, Any] = {"split": split, "rules": {}, "settings": vars(args)}
    for rule in rules:
        detailed["rules"][rule.name] = {"kind": rule.kind, "params": rule.params, "parts": {}}
        for part_name, part_rows in replay_partitions.items():
            admissions = admission_times(part_rows, rule)
            if args.exact_replay:
                signals = replay_for_admissions(
                    part_rows,
                    scores_by_part[part_name],
                    high,
                    scorer,
                    admissions,
                    replay_args,
                )
            else:
                signals = filter_signals_after_admission(baseline_signals_by_part[part_name], admissions)
            metrics = evaluate_rule(part_rows, admissions, signals)
            detailed["rules"][rule.name]["parts"][part_name] = metrics
            row = flatten_metrics(rule, part_name, metrics)
            rows.append(row)
        for part_name, part_rows in admission_only_partitions.items():
            admissions = admission_times(part_rows, rule)
            metrics = evaluate_rule(part_rows, admissions, pd.DataFrame())
            detailed["rules"][rule.name]["parts"][part_name] = metrics
            row = flatten_metrics(rule, part_name, metrics)
            rows.append(row)

    table = pd.DataFrame(rows)
    val_ranked = rank_rules(table[table["part"] == "val"].copy())
    ranked_names = val_ranked["rule"].head(12).tolist()
    table["val_rank_sort"] = table["rule"].map(dict(zip(val_ranked["rule"], val_ranked["val_rank_sort"])))
    selected = table[table["rule"].isin(ranked_names)].sort_values(["val_rank_sort", "part"], na_position="last")

    out_json = out_dir / "pump_admission_optimization.json"
    out_md = out_dir / "pump_admission_optimization.md"
    out_csv = out_dir / "pump_admission_optimization.csv"
    out_json.write_text(json.dumps(detailed, ensure_ascii=False, indent=2), encoding="utf-8")
    table.to_csv(out_csv, index=False)
    out_md.write_text(render_markdown(table, val_ranked, selected, args), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "markdown": str(out_md), "csv": str(out_csv)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize PumpWatch admission thresholds.")
    parser.add_argument("--dense", default="backend/storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--high-pump-dense", default="backend/storage/ml/high_pump40_experts/high_pump_40_dense.parquet")
    parser.add_argument("--models-dir", default="backend/pump_dump_hunter/ml/models")
    parser.add_argument("--out-dir", default="backend/storage/ml/pump_admission_optimization")
    parser.add_argument("--confirm-bars", type=int, default=2)
    parser.add_argument("--cooldown-bars", type=int, default=8)
    parser.add_argument("--margin", type=float, default=life.DEFAULT_ROUTE_MARGIN)
    parser.add_argument("--dynamic-thresholds", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--fast-trend-threshold", type=float, default=0.97)
    parser.add_argument("--slow-trend-threshold", type=float, default=0.82)
    parser.add_argument("--fast-break-threshold", type=float, default=life.DEFAULT_ROUTE_THRESHOLDS["fast_dump"])
    parser.add_argument("--slow-break-threshold", type=float, default=life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])
    parser.add_argument("--slow-mature-threshold", type=float, default=life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])
    parser.add_argument("--exact-replay", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--rules", default="", help="Comma-separated rule names to evaluate.")
    return parser.parse_args(argv)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.exists() or path.is_absolute():
        return path
    alt = Path("backend") / path
    return alt if alt.exists() else path


def build_rules() -> list[AdmissionRule]:
    rules: list[AdmissionRule] = []
    for pct in (20, 22, 24, 25, 26, 28, 30, 35, 40, 45, 50, 55, 60):
        rules.append(AdmissionRule(f"ctx_high_{pct}", "ctx_high", {"min_high": pct / 100.0}))
    window_profiles = {
        "current_proxy": {"ret_1": 0.08, "ret_2": 0.10, "ret_24": 0.20, "ret_48": 0.30, "ret_96": 0.40},
        "balanced": {"ret_1": 0.10, "ret_2": 0.15, "ret_24": 0.25, "ret_48": 0.35, "ret_96": 0.45},
        "strict": {"ret_1": 0.12, "ret_2": 0.18, "ret_24": 0.30, "ret_48": 0.40, "ret_96": 0.50},
        "very_strict": {"ret_1": 0.15, "ret_2": 0.22, "ret_24": 0.35, "ret_48": 0.45, "ret_96": 0.60},
        "short_burst": {"ret_1": 0.10, "ret_2": 0.15, "ret_24": None, "ret_48": None, "ret_96": None},
        "hard_burst": {"ret_1": 0.12, "ret_2": 0.18, "ret_24": None, "ret_48": None, "ret_96": None},
        "life25_loose": {"ret_1": 0.10, "ret_2": 0.15, "ret_24": 0.25, "ret_48": 0.25, "ret_96": 0.25},
        "life25_strict": {"ret_1": 0.12, "ret_2": 0.18, "ret_24": 0.25, "ret_48": 0.25, "ret_96": 0.25},
    }
    for name, params in window_profiles.items():
        rules.append(AdmissionRule(f"window_{name}", "window", params))
    for pct in (30, 35, 40, 45):
        rules.append(AdmissionRule(f"combo_ctx{pct}_balanced", "combo", {"min_high": pct / 100.0, **window_profiles["balanced"]}))
        rules.append(AdmissionRule(f"combo_ctx{pct}_strict", "combo", {"min_high": pct / 100.0, **window_profiles["strict"]}))
    return rules


def window_mask(rows: pd.DataFrame, params: dict[str, float | None]) -> pd.Series:
    mask = pd.Series(False, index=rows.index)
    for col, threshold in params.items():
        if col == "min_high" or threshold is None or col not in rows:
            continue
        mask |= rows[col].fillna(-999.0) >= float(threshold)
    return mask


def admission_times(rows: pd.DataFrame, rule: AdmissionRule) -> dict[str, int]:
    qualified = rows[rule.mask(rows)].sort_values(["life_id", "decision_time"])
    if qualified.empty:
        return {}
    first = qualified.groupby("life_id", as_index=False).first()
    return dict(zip(first["life_id"].astype(str), first["decision_time"].astype("int64")))


def first_row_times(rows: pd.DataFrame) -> dict[str, int]:
    first = rows.sort_values(["life_id", "decision_time"]).groupby("life_id", as_index=False).first()
    return dict(zip(first["life_id"].astype(str), first["decision_time"].astype("int64")))


def filter_signals_after_admission(signals: pd.DataFrame, admissions: dict[str, int]) -> pd.DataFrame:
    if signals.empty or not admissions:
        return pd.DataFrame()
    admit_time = signals["life_id"].astype(str).map(admissions)
    return signals[admit_time.notna() & (signals["decision_time"] >= admit_time)].copy()


def load_high_pump(path: Path, dense: pd.DataFrame) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    high = pd.read_parquet(path).copy()
    if "source_life_id" not in high:
        high["source_life_id"] = high["life_id"].astype(str).str.replace(r"\|high\d+$", "", regex=True)
    states = high.apply(life.assign_behavior_state, axis=1)
    high["behavior_state"] = states
    for behavior in life.BEHAVIOR_ORDER:
        high[f"behavior_{behavior}"] = (states == behavior).astype("int8")
    add_slow_features_vectorized(high)
    source_meta = dense[["life_id", "entry_time"]].drop_duplicates()
    return high.merge(source_meta.rename(columns={"life_id": "source_life_id", "entry_time": "source_entry_time"}), on="source_life_id", how="left")


def replay_for_admissions(
    rows: pd.DataFrame,
    scores: dict[str, Any],
    high: pd.DataFrame,
    scorer: MLScorer,
    admissions: dict[str, int],
    args: SimpleNamespace,
) -> pd.DataFrame:
    if not admissions:
        return pd.DataFrame()
    admit_series = rows["life_id"].astype(str).map(admissions)
    eligible = rows[admit_series.notna() & (rows["decision_time"] >= admit_series)].copy()
    if eligible.empty:
        return pd.DataFrame()
    router = replay_router_strategy(eligible, scores, scorer, args.confirm_bars, args.cooldown_bars, args.margin, args)
    high_replay = replay_high_after_admission(high, scorer, admissions, args)
    return combine_signal_streams(router, high_replay, args.cooldown_bars)


def replay_high_after_admission(high: pd.DataFrame, scorer: MLScorer, admissions: dict[str, int], args: SimpleNamespace) -> pd.DataFrame:
    if high.empty or not {"high_top", "high_short"}.issubset(scorer._lifecycle_boosters):
        return pd.DataFrame()
    work = high[high["source_life_id"].astype(str).isin(admissions.keys())].copy()
    if work.empty:
        return pd.DataFrame()
    admit_time = work["source_life_id"].astype(str).map(admissions)
    work = work[work["decision_time"] >= admit_time].copy()
    if work.empty:
        return pd.DataFrame()
    cols = list(set(scorer.lifecycle_columns("high_top") + scorer.lifecycle_columns("high_short")))
    work = work.dropna(subset=cols, how="any")
    if work.empty:
        return pd.DataFrame()
    scores = score_high_pump(work, scorer)
    top_thr = scorer.lifecycle_threshold("high_top") or 1.0
    short_thr = scorer.lifecycle_threshold("high_short") or 1.0
    signals: list[dict[str, Any]] = []
    for source_life_id, group in work.sort_values(["source_life_id", "decision_time"]).groupby("source_life_id", sort=False):
        last_signal_bar: dict[str, int] = {}
        emitted_once: set[str] = set()
        for bar_ix, (idx, row) in enumerate(group.iterrows()):
            state = str(row["behavior_state"])
            emitted = None
            model = ""
            score = np.nan
            threshold = np.nan
            if (
                state in life.HIGH_PUMP_SHORT_GATE
                and life.high_pump_short_setup(row)
                and "high_short" in scores
                and float(scores["high_short"].loc[idx]) >= short_thr
            ):
                emitted = "short_signal"
                model = "high_short"
                score = float(scores["high_short"].loc[idx])
                threshold = short_thr
            elif (
                state in life.HIGH_PUMP_TOP_GATE
                and life.high_pump_top_setup(row)
                and "high_top" in scores
                and float(scores["high_top"].loc[idx]) >= top_thr
            ):
                emitted = "early_alert"
                model = "high_top"
                score = float(scores["high_top"].loc[idx])
                threshold = top_thr
            if emitted is None or emitted in emitted_once:
                continue
            last = last_signal_bar.get(emitted)
            if last is not None and bar_ix - last < args.cooldown_bars:
                continue
            last_signal_bar[emitted] = bar_ix
            emitted_once.add(emitted)
            signals.append(
                signal_row(
                    row,
                    source_life_id,
                    emitted,
                    model,
                    score,
                    threshold,
                    "high_pump",
                    {"confidence": score, "margin": 0.0},
                    1,
                )
            )
    return pd.DataFrame(signals)


def evaluate_rule(rows: pd.DataFrame, admissions: dict[str, int], signals: pd.DataFrame) -> dict[str, Any]:
    total_lives = rows["life_id"].nunique()
    admitted_ids = set(admissions.keys())
    admitted_rows = first_admission_rows(rows, admissions)
    signal_first = first_per_lifecycle(signals)
    summary = summarize_signals(signal_first)
    return {
        "lifecycles": int(total_lives),
        "admitted": int(len(admitted_ids)),
        "admission_rate": safe_div(len(admitted_ids), total_lives),
        "admission": summarize_admission(rows, admitted_rows),
        "signals_first": summary,
        "signals_total": summarize_signals(signals),
        "signal_count": int(len(signal_first)),
        "short_signal_count": int((signal_first["level"] == "short_signal").sum()) if len(signal_first) else 0,
        "early_alert_count": int((signal_first["level"] == "early_alert").sum()) if len(signal_first) else 0,
        "family_admitted": admitted_rows["family"].value_counts().to_dict() if len(admitted_rows) else {},
    }


def first_admission_rows(rows: pd.DataFrame, admissions: dict[str, int]) -> pd.DataFrame:
    if not admissions:
        return pd.DataFrame()
    work = rows[rows["life_id"].astype(str).isin(admissions.keys())].copy()
    work = work[work["decision_time"] == work["life_id"].astype(str).map(admissions)]
    return work.sort_values(["life_id", "decision_time"]).groupby("life_id", as_index=False).first()


def summarize_admission(rows: pd.DataFrame, admitted: pd.DataFrame) -> dict[str, Any]:
    if admitted.empty:
        return {}
    peaks = peak_rows(rows)
    merged = admitted.merge(peaks, on="life_id", suffixes=("", "_peak"), how="left")
    return {
        "median_delay_h": median((merged["decision_time"] - merged["entry_time"]) / 3_600_000),
        "median_delay_from_peak_h": median((merged["decision_time"] - merged["peak_time"]) / 3_600_000),
        "before_peak_rate": safe_div(float((merged["decision_time"] <= merged["peak_time"]).sum()), len(merged)),
        "median_ctx_high": median(merged["ctx_high_since_entry"]),
        "median_future_drop_24h": median(merged["future_drop_24h"]),
        "median_future_drop_72h": median(merged["future_drop_72h"]),
        "median_future_up_24h": median(merged["future_up_24h"]),
        "median_short_adverse_24h": median(merged["short_adverse_before_down5_24h"]),
    }


def peak_rows(rows: pd.DataFrame) -> pd.DataFrame:
    idx = rows.sort_values(["life_id", "current_price"]).groupby("life_id")["current_price"].idxmax()
    peaks = rows.loc[idx, ["life_id", "decision_time", "ctx_high_since_entry", "current_price"]].copy()
    peaks = peaks.rename(columns={"decision_time": "peak_time", "ctx_high_since_entry": "peak_ctx_high", "current_price": "peak_price"})
    return peaks


def flatten_metrics(rule: AdmissionRule, part: str, metrics: dict[str, Any]) -> dict[str, Any]:
    admission = metrics.get("admission") or {}
    sig = metrics.get("signals_first") or {}
    short = sig.get("short_signal_total", {})
    early = sig.get("early_alert_total", {})
    return {
        "rule": rule.name,
        "part": part,
        "kind": rule.kind,
        "admitted": metrics.get("admitted", 0),
        "admission_rate": metrics.get("admission_rate", 0.0),
        "admit_delay_h": admission.get("median_delay_h"),
        "admit_delay_from_peak_h": admission.get("median_delay_from_peak_h"),
        "admit_before_peak_rate": admission.get("before_peak_rate"),
        "admit_drop24": admission.get("median_future_drop_24h"),
        "admit_up24": admission.get("median_future_up_24h"),
        "signals": metrics.get("signal_count", 0),
        "early": metrics.get("early_alert_count", 0),
        "shorts": metrics.get("short_signal_count", 0),
        "short_drop24": short.get("median_drop_24h"),
        "short_drop72": short.get("median_drop_72h"),
        "short_adv24": short.get("median_short_adverse_24h"),
        "short_clean24": short.get("clean_short_24h_rate"),
        "early_drop24": early.get("median_drop_24h"),
        "early_adv24": early.get("median_short_adverse_24h"),
    }


def rank_rules(val: pd.DataFrame) -> pd.DataFrame:
    ranked = val.copy()
    ranked["short_drop24_f"] = ranked["short_drop24"].fillna(0.0)
    ranked["short_adv24_f"] = ranked["short_adv24"].fillna(0.30)
    ranked["short_clean24_f"] = ranked["short_clean24"].fillna(0.0)
    ranked["early_drop24_f"] = ranked["early_drop24"].fillna(0.0)
    ranked["admit_rate_f"] = ranked["admission_rate"].fillna(0.0)
    ranked["score"] = (
        ranked["short_drop24_f"] * 2.0
        - ranked["short_adv24_f"] * 1.5
        + ranked["short_clean24_f"] * 0.5
        + ranked["early_drop24_f"] * 0.4
        + np.minimum(ranked["shorts"], 6) * 0.03
        - np.maximum(ranked["admit_rate_f"] - 0.80, 0) * 0.4
    )
    ranked.loc[ranked["shorts"] < 2, "score"] -= 0.25
    ranked = ranked.sort_values(["score", "shorts", "short_drop24"], ascending=[False, False, False]).reset_index(drop=True)
    ranked["val_rank_sort"] = np.arange(len(ranked))
    return ranked


def render_markdown(table: pd.DataFrame, val_ranked: pd.DataFrame, selected: pd.DataFrame, args: argparse.Namespace) -> str:
    val_rank_map = dict(zip(val_ranked["rule"], val_ranked["val_rank_sort"]))
    table = table.copy()
    table["val_rank_sort"] = table["rule"].map(val_rank_map)
    selected = table[table["rule"].isin(val_ranked["rule"].head(12))].sort_values(["val_rank_sort", "part"])
    lines = [
        "# PumpWatch Admission Optimization",
        "",
        "This experiment replays the current production lifecycle models after different simulated PumpWatch admission rules.",
        "",
        f"- Dense: `{args.dense}`",
        f"- Cooldown bars: `{args.cooldown_bars}`",
        f"- Confirm bars: `{args.confirm_bars}`",
        f"- Exact replay per rule: `{args.exact_replay}`",
        "",
        "## Top Validation Rules",
        "",
    ]
    lines.extend(render_table(val_ranked.head(15)))
    lines += ["", "## Validation Top Rules Checked On Test And All-Admission", ""]
    lines.extend(render_table(selected))
    lines += [
        "",
        "## Notes",
        "",
        "- `ctx_high_N` means the lifecycle must have reached N% from its original anchor before entering PumpWatch.",
        "- `window_*` uses closed 15m returns: ret_1=15m, ret_2=30m, ret_24=6h, ret_48=12h, ret_96=24h.",
        "- `combo_*` admits either after the lifecycle high threshold or a burst/window profile.",
        "- Ranking is validation-only; test rows are used as confirmation, not as the selector.",
        "- `all_admission` does not replay model signals; it only reports how broad/timely the admission rule is across all lifecycles.",
        "- With exact replay disabled, each rule filters a single baseline replay by admission time. This is used for fast screening; exact replay should be used for final candidates.",
    ]
    return "\n".join(lines) + "\n"


def render_table(rows: pd.DataFrame) -> list[str]:
    cols = [
        "rule",
        "part",
        "admitted",
        "admission_rate",
        "admit_delay_from_peak_h",
        "signals",
        "early",
        "shorts",
        "short_drop24",
        "short_adv24",
        "short_clean24",
        "early_drop24",
        "score",
    ]
    available = [c for c in cols if c in rows.columns]
    out = ["| " + " | ".join(available) + " |", "| " + " | ".join("---" for _ in available) + " |"]
    for _, row in rows[available].iterrows():
        out.append("| " + " | ".join(format_cell(row[c], c) for c in available) + " |")
    return out


def format_cell(value: Any, column: str = "") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "-"
    if isinstance(value, (np.floating, float)):
        if column.endswith("_h"):
            return f"{value:.2f}"
        if (
            column.endswith("rate")
            or "drop" in column
            or "up" in column
            or "adv" in column
            or column in {"score", "admission_rate", "short_clean24", "early_drop24", "short_drop24", "short_drop72"}
        ):
            return f"{value * 100:.1f}%"
        return f"{value:.2f}"
    return str(value)


def median(value: Any) -> float | None:
    arr = pd.Series(value).replace([np.inf, -np.inf], np.nan).dropna()
    if arr.empty:
        return None
    return float(arr.median())


def safe_div(num: float, den: float) -> float:
    return float(num / den) if den else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
