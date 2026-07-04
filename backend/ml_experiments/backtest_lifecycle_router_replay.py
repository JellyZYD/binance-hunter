"""Replay the production lifecycle router/expert strategy on dense 15m rows.

The script intentionally loads the same model files and router helper used by
live inference. It does not retrain models. Each dense row represents a closed
15m decision point with only past-derived features plus future evaluation
columns used for reporting.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pump_dump_hunter.ml import lifecycle as life
from pump_dump_hunter.ml.model import MLScorer


HORIZONS = ("1h", "3h", "6h", "12h", "24h", "72h")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dense_path = resolve_path(args.dense)
    models_dir = resolve_path(args.models_dir)
    out_dir = resolve_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dense = pd.read_parquet(dense_path).copy()
    dense = prepare_dense(dense)
    split = split_times(dense)
    test = dense[dense["entry_time"] >= split["test_start"]].copy()

    scorer = MLScorer(models_dir)
    if not scorer.lifecycle_ready or not scorer.lifecycle_router_ready:
        raise RuntimeError(f"lifecycle models not ready: {scorer.error}")

    scores = score_all(test, scorer)
    replay = replay_router_strategy(test, scores, scorer, args.confirm_bars, args.cooldown_bars, args.margin, args)
    high_replay = replay_high_pump_strategy(dense, test, scorer, args) if args.high_pump_enabled else pd.DataFrame()
    combined = combine_signal_streams(replay, high_replay, args.cooldown_bars)
    report = build_report(test, replay, high_replay, combined, split, args)

    out_json = out_dir / "lifecycle_router_replay.json"
    out_md = out_dir / "lifecycle_router_replay.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(out_json), "report": str(out_md)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay production lifecycle router strategy.")
    parser.add_argument("--dense", default="backend/storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--models-dir", default="backend/pump_dump_hunter/ml/models")
    parser.add_argument("--out-dir", default="backend/storage/ml/lifecycle_router_replay")
    parser.add_argument("--confirm-bars", type=int, default=2)
    parser.add_argument("--cooldown-bars", type=int, default=8, help="2h on 15m bars.")
    parser.add_argument("--margin", type=float, default=life.DEFAULT_ROUTE_MARGIN)
    parser.add_argument("--dynamic-thresholds", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--fast-trend-threshold", type=float, default=0.97)
    parser.add_argument("--slow-trend-threshold", type=float, default=0.82)
    parser.add_argument("--fast-break-threshold", type=float, default=life.DEFAULT_ROUTE_THRESHOLDS["fast_dump"])
    parser.add_argument("--slow-break-threshold", type=float, default=life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])
    parser.add_argument("--slow-mature-threshold", type=float, default=life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])
    parser.add_argument("--high-pump-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--high-pump-dense", default="backend/storage/ml/high_pump40_experts/high_pump_40_dense.parquet")
    return parser.parse_args(argv)


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.exists() or path.is_absolute():
        return path
    alt = Path("backend") / path
    return alt if alt.exists() else path


def split_times(dense: pd.DataFrame) -> dict[str, int]:
    unique_times = np.sort(dense["entry_time"].dropna().unique())
    q70, q85 = np.quantile(unique_times, [0.70, 0.85])
    embargo = 3 * 86_400_000
    return {
        "train_until": int(q70),
        "val_start": int(q70 + embargo),
        "val_until": int(q85),
        "test_start": int(q85 + embargo),
    }


def prepare_dense(dense: pd.DataFrame) -> pd.DataFrame:
    dense = dense.sort_values(["entry_time", "symbol", "decision_time"]).reset_index(drop=True)
    dense["life_id"] = dense["linked_event_id"].fillna("")
    missing_id = dense["life_id"].astype(str).str.len() == 0
    dense.loc[missing_id, "life_id"] = (
        dense.loc[missing_id, "symbol"].astype(str) + "-" + dense.loc[missing_id, "entry_time"].astype(str)
    )
    states = dense.apply(life.assign_behavior_state, axis=1)
    dense["behavior_state"] = states
    for behavior in life.BEHAVIOR_ORDER:
        dense[f"behavior_{behavior}"] = (states == behavior).astype("int8")
    add_slow_features_vectorized(dense)
    return dense


def add_slow_features_vectorized(df: pd.DataFrame) -> None:
    noise = np.maximum(df["atr_14"].fillna(0).to_numpy(float), df["retstd_20"].fillna(0).to_numpy(float) * 1.5)
    noise = np.clip(noise, 0.006, 0.10)
    amp = np.maximum(df["ctx_high_since_entry"].fillna(0).to_numpy(float), 0.0)
    ret = df["ctx_ret_since_entry"].fillna(0).to_numpy(float)
    drawdown = np.maximum(-df["ctx_drawdown_from_entry_high"].fillna(0).to_numpy(float), 0.0)
    red = df["ctx_red_bar_share"].fillna(0).to_numpy(float)
    tsell = df["ctx_taker_sell_mean"].fillna(0.5).to_numpy(float)
    qv_recent = np.maximum(df["ctx_qv_recent_ratio"].fillna(1.0).to_numpy(float), 0.0)
    hours = np.maximum(df["ctx_hours_since_entry"].fillna(0).to_numpy(float), 0.0)
    sell_pressure = np.maximum(red - 0.45, 0.0) + np.maximum(tsell - 0.50, 0.0)
    df["slow_noise"] = noise
    df["slow_amp"] = amp
    df["slow_ret"] = ret
    df["slow_drawdown"] = drawdown
    df["slow_drawdown_over_amp"] = drawdown / np.maximum(amp, 0.03)
    df["slow_drawdown_over_noise"] = drawdown / np.maximum(noise, 0.006)
    df["slow_hours_log"] = np.log1p(hours)
    df["slow_sell_pressure"] = sell_pressure
    df["slow_range_pressure"] = sell_pressure * np.log1p(qv_recent) * np.sqrt(np.maximum(drawdown, 0.0))
    df["slow_ret6_over_noise"] = df["ret_6"].fillna(0).to_numpy(float) / np.maximum(noise, 0.006)
    df["slow_dist21_over_noise"] = df["dist_ema21"].fillna(0).to_numpy(float) / np.maximum(noise, 0.006)
    df["slow_maturity"] = np.log1p(hours) * np.log1p(np.maximum(amp * 10.0, 0.0))


def score_all(rows: pd.DataFrame, scorer: MLScorer) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("family_router", "fast_top", "fast_short", "slow_warning", "slow_short"):
        cols = scorer.lifecycle_columns(name)
        x = rows[cols].to_numpy(dtype="float64", copy=True)
        finite = np.isfinite(x).all(axis=1)
        pred = scorer._lifecycle_boosters[name].predict(x)
        if name == "family_router":
            arr = np.asarray(pred, dtype="float64")
            classes = scorer.lifecycle_meta["models"][name]["classes"]
            probs = pd.DataFrame(arr, columns=classes, index=rows.index)
            probs.loc[~finite, :] = np.nan
            out[name] = probs
        else:
            arr = np.asarray(pred, dtype="float64").reshape(-1)
            arr[~finite] = np.nan
            out[name] = pd.Series(arr, index=rows.index)
    return out


def score_high_pump(rows: pd.DataFrame, scorer: MLScorer) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for name in ("high_top", "high_short"):
        if name not in scorer._lifecycle_boosters:
            continue
        cols = scorer.lifecycle_columns(name)
        x = rows[cols].to_numpy(dtype="float64", copy=True)
        finite = np.isfinite(x).all(axis=1)
        arr = np.asarray(scorer._lifecycle_boosters[name].predict(x), dtype="float64").reshape(-1)
        arr[~finite] = np.nan
        out[name] = pd.Series(arr, index=rows.index)
    return out


def dynamic_thresholds(row: pd.Series, base: dict[str, float], args: argparse.Namespace) -> dict[str, float]:
    thresholds = dict(base)
    if not args.dynamic_thresholds:
        return thresholds
    state = str(row.get("behavior_state", "neutral_watch") or "neutral_watch")
    high = max(float(row.get("ctx_high_since_entry", 0.0) or 0.0), 0.0)
    drawdown = max(-float(row.get("ctx_drawdown_from_entry_high", 0.0) or 0.0), 0.0)
    hours = max(float(row.get("ctx_hours_since_entry", 0.0) or 0.0), 0.0)
    if state in {"acceleration", "trend_hold"}:
        thresholds["fast_dump"] = max(thresholds["fast_dump"], float(args.fast_trend_threshold))
        thresholds["slow_distribution"] = max(thresholds["slow_distribution"], float(args.slow_trend_threshold))
    if state in {"pullback_risk", "breakdown"} and high >= 0.14:
        thresholds["fast_dump"] = min(thresholds["fast_dump"], float(args.fast_break_threshold))
    if state == "breakdown" and high >= 0.16 and drawdown >= 0.08:
        thresholds["slow_distribution"] = min(thresholds["slow_distribution"], float(args.slow_break_threshold))
    elif state == "distribution" and high >= 0.18 and hours >= 12:
        thresholds["slow_distribution"] = min(thresholds["slow_distribution"], float(args.slow_mature_threshold))
    return thresholds


def replay_router_strategy(
    rows: pd.DataFrame,
    scores: dict[str, Any],
    scorer: MLScorer,
    confirm_bars: int,
    cooldown_bars: int,
    margin: float,
    args: argparse.Namespace,
) -> pd.DataFrame:
    base_thresholds = dict(life.DEFAULT_ROUTE_THRESHOLDS)
    route_meta = (scorer.lifecycle_meta or {}).get("route", {})
    base_thresholds.update(route_meta.get("thresholds", {}))
    fast_top_thr = scorer.lifecycle_threshold("fast_top") or 1.0
    fast_short_thr = scorer.lifecycle_threshold("fast_short") or 1.0
    slow_warning_thr = scorer.lifecycle_threshold("slow_warning") or 1.0
    slow_short_thr = scorer.lifecycle_threshold("slow_short") or 1.0

    signals: list[dict[str, Any]] = []
    probs_df: pd.DataFrame = scores["family_router"]
    grouped = rows.sort_values(["life_id", "decision_time"]).groupby("life_id", sort=False)
    for life_id, group in grouped:
        candidate = "unknown"
        streak = 0
        last_signal_bar: dict[str, int] = {}
        for bar_ix, (idx, row) in enumerate(group.iterrows()):
            probs = probs_df.loc[idx].dropna().to_dict()
            if not probs:
                route = {"mode": "unknown", "candidate": "unknown", "confidence": 0.0, "margin": 0.0, "probs": {}}
            else:
                route = life.route_from_probabilities(
                    probs,
                    thresholds=dynamic_thresholds(row, base_thresholds, args),
                    margin_threshold=margin,
                )
            raw_mode = str(route["mode"] or "unknown")
            if raw_mode == "unknown":
                candidate = "unknown"
                streak = 0
                confirmed = "unknown"
            else:
                streak = streak + 1 if candidate == raw_mode else 1
                candidate = raw_mode
                confirmed = raw_mode if streak >= max(1, confirm_bars) else "unknown"
            state = str(row["behavior_state"])
            if confirmed in {"unknown", "continuation", "second_distribution"}:
                continue
            emitted = None
            model = ""
            score = np.nan
            threshold = np.nan
            if confirmed == "fast_dump":
                fs = float(scores["fast_short"].loc[idx])
                ft = float(scores["fast_top"].loc[idx])
                if state in life.FAST_SHORT_GATE and fs >= fast_short_thr:
                    emitted = "short_signal"
                    model = "fast_short"
                    score = fs
                    threshold = fast_short_thr
                elif state in life.FAST_TOP_GATE and ft >= fast_top_thr:
                    emitted = "early_alert"
                    model = "fast_top"
                    score = ft
                    threshold = fast_top_thr
            elif confirmed == "slow_distribution":
                ss = float(scores["slow_short"].loc[idx])
                sw = float(scores["slow_warning"].loc[idx])
                if state in life.SLOW_SHORT_GATE and ss >= slow_short_thr:
                    emitted = "short_signal"
                    model = "slow_short"
                    score = ss
                    threshold = slow_short_thr
                elif state in life.SLOW_TOP_GATE and sw >= slow_warning_thr:
                    emitted = "distribution_warning"
                    model = "slow_warning"
                    score = sw
                    threshold = slow_warning_thr
            if emitted is None:
                continue
            last = last_signal_bar.get(emitted)
            if last is not None and bar_ix - last < cooldown_bars:
                continue
            last_signal_bar[emitted] = bar_ix
            signals.append(signal_row(row, life_id, emitted, model, score, threshold, confirmed, route, streak))
    return pd.DataFrame(signals)


def replay_high_pump_strategy(
    dense: pd.DataFrame,
    test: pd.DataFrame,
    scorer: MLScorer,
    args: argparse.Namespace,
) -> pd.DataFrame:
    high_path = resolve_path(args.high_pump_dense)
    if not high_path.exists() or not {"high_top", "high_short"}.issubset(scorer._lifecycle_boosters):
        return pd.DataFrame()
    high = pd.read_parquet(high_path).copy()
    if "source_life_id" not in high:
        high["source_life_id"] = high["life_id"].astype(str).str.replace(r"\|high\d+$", "", regex=True)
    high = high[high["source_life_id"].astype(str).isin(set(test["life_id"].astype(str)))].copy()
    if high.empty:
        return pd.DataFrame()
    states = high.apply(life.assign_behavior_state, axis=1)
    high["behavior_state"] = states
    for behavior in life.BEHAVIOR_ORDER:
        high[f"behavior_{behavior}"] = (states == behavior).astype("int8")
    add_slow_features_vectorized(high)
    high = high.dropna(subset=list(set(scorer.lifecycle_columns("high_top") + scorer.lifecycle_columns("high_short"))), how="any")
    if high.empty:
        return pd.DataFrame()
    scores = score_high_pump(high, scorer)
    top_thr = scorer.lifecycle_threshold("high_top") or 1.0
    short_thr = scorer.lifecycle_threshold("high_short") or 1.0
    signals: list[dict[str, Any]] = []
    grouped = high.sort_values(["source_life_id", "decision_time"]).groupby("source_life_id", sort=False)
    for life_id, group in grouped:
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
            if emitted is None:
                continue
            if emitted in emitted_once:
                continue
            last = last_signal_bar.get(emitted)
            if last is not None and bar_ix - last < args.cooldown_bars:
                continue
            last_signal_bar[emitted] = bar_ix
            emitted_once.add(emitted)
            signals.append(
                signal_row(
                    row,
                    life_id,
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


def combine_signal_streams(router: pd.DataFrame, high_pump: pd.DataFrame, cooldown_bars: int) -> pd.DataFrame:
    frames = []
    if not high_pump.empty:
        hp = high_pump.copy()
        hp["stream_priority"] = 0
        frames.append(hp)
    if not router.empty:
        rt = router.copy()
        rt["stream_priority"] = 1
        frames.append(rt)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False).sort_values(["life_id", "decision_time", "stream_priority"])
    keep: list[pd.Series] = []
    last_time: dict[tuple[str, str], int] = {}
    cooldown_ms = int(cooldown_bars) * 15 * 60_000
    for _, row in merged.iterrows():
        key = (str(row["life_id"]), str(row["level"]))
        previous = last_time.get(key)
        if previous is not None and int(row["decision_time"]) - previous < cooldown_ms:
            continue
        last_time[key] = int(row["decision_time"])
        keep.append(row)
    if not keep:
        return pd.DataFrame()
    return pd.DataFrame(keep).drop(columns=["stream_priority"], errors="ignore").reset_index(drop=True)


def signal_row(
    row: pd.Series,
    life_id: Any,
    level: str,
    model: str,
    score: float,
    threshold: float,
    route_mode: str,
    route: dict[str, Any],
    streak: int,
) -> dict[str, Any]:
    out = {
        "life_id": str(life_id),
        "symbol": row["symbol"],
        "entry_time": int(row["entry_time"]),
        "decision_time": int(row["decision_time"]),
        "level": level,
        "model": model,
        "score": float(score),
        "threshold": float(threshold),
        "behavior_state": row["behavior_state"],
        "route_mode": route_mode,
        "route_confidence": float(route["confidence"]),
        "route_margin": float(route["margin"]),
        "route_streak": int(streak),
        "family": row.get("family", ""),
        "current_price": float(row.get("current_price", np.nan)),
    }
    for horizon in HORIZONS:
        for prefix in ("future_up", "future_drop", "short_adverse_before_down5"):
            col = f"{prefix}_{horizon}"
            if col in row:
                out[col] = float(row[col])
    return out


def build_report(
    test: pd.DataFrame,
    router_signals: pd.DataFrame,
    high_signals: pd.DataFrame,
    combined_signals: pd.DataFrame,
    split: dict[str, int],
    args: argparse.Namespace,
) -> dict[str, Any]:
    signals = combined_signals
    signal_summary = summarize_signals(signals)
    first = first_per_lifecycle(signals)
    return {
        "strategy": "lifecycle_router_expert_high_pump",
        "dense_rows_test": int(len(test)),
        "lifecycles_test": int(test["life_id"].nunique()),
        "split": split,
        "settings": {
            "confirm_bars": args.confirm_bars,
            "cooldown_bars": args.cooldown_bars,
            "route_margin": args.margin,
            "dynamic_thresholds": args.dynamic_thresholds,
            "dynamic_values": {
                "fast_trend_threshold": args.fast_trend_threshold,
                "slow_trend_threshold": args.slow_trend_threshold,
                "fast_break_threshold": args.fast_break_threshold,
                "slow_break_threshold": args.slow_break_threshold,
                "slow_mature_threshold": args.slow_mature_threshold,
            },
            "router_thresholds": life.DEFAULT_ROUTE_THRESHOLDS,
            "slow_short_gate": sorted(life.SLOW_SHORT_GATE),
            "fast_short_gate": sorted(life.FAST_SHORT_GATE),
            "high_pump_enabled": args.high_pump_enabled,
            "high_pump_dense": args.high_pump_dense,
            "high_pump_top_gate": sorted(life.HIGH_PUMP_TOP_GATE),
            "high_pump_short_gate": sorted(life.HIGH_PUMP_SHORT_GATE),
        },
        "signals_router_only": summarize_signals(router_signals),
        "signals_high_pump_only": summarize_signals(high_signals),
        "signals_all": signal_summary,
        "signals_first_per_lifecycle_level": summarize_signals(first),
        "route_counts": signals["route_mode"].value_counts().to_dict() if len(signals) else {},
        "state_counts": signals["behavior_state"].value_counts().to_dict() if len(signals) else {},
        "sample_signals": signals.head(25).to_dict(orient="records") if len(signals) else [],
    }


def first_per_lifecycle(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return signals
    return signals.sort_values("decision_time").groupby(["life_id", "level"], as_index=False).head(1)


def summarize_signals(signals: pd.DataFrame) -> dict[str, Any]:
    if signals.empty:
        return {"total": 0}
    out: dict[str, Any] = {"total": int(len(signals))}
    for key, group in signals.groupby(["level", "model", "route_mode"], dropna=False):
        name = "|".join(str(x) for x in key)
        out[name] = metrics(group)
    out["short_signal_total"] = metrics(signals[signals["level"] == "short_signal"])
    out["early_alert_total"] = metrics(signals[signals["level"] == "early_alert"])
    out["distribution_warning_total"] = metrics(signals[signals["level"] == "distribution_warning"])
    return out


def metrics(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {"signals": 0}
    result: dict[str, Any] = {
        "signals": int(len(rows)),
        "lifecycles": int(rows["life_id"].nunique()),
        "median_route_confidence": safe_median(rows["route_confidence"]),
        "median_route_margin": safe_median(rows["route_margin"]),
        "states": rows["behavior_state"].value_counts().to_dict(),
        "families": rows["family"].value_counts().to_dict(),
    }
    for horizon in HORIZONS:
        up = f"future_up_{horizon}"
        drop = f"future_drop_{horizon}"
        adv = f"short_adverse_before_down5_{horizon}"
        if up in rows:
            result[f"median_up_{horizon}"] = safe_median(rows[up])
        if drop in rows:
            result[f"median_drop_{horizon}"] = safe_median(rows[drop])
            result[f"drop_ge_8pct_{horizon}"] = float((rows[drop] >= 0.08).mean())
            result[f"drop_ge_15pct_{horizon}"] = float((rows[drop] >= 0.15).mean())
        if adv in rows:
            result[f"median_short_adverse_{horizon}"] = safe_median(rows[adv])
            result[f"short_adverse_le_6pct_{horizon}"] = float((rows[adv] <= 0.06).mean())
    if "future_drop_24h" in rows and "short_adverse_before_down5_24h" in rows:
        result["clean_short_24h_rate"] = float(((rows["future_drop_24h"] >= 0.08) & (rows["short_adverse_before_down5_24h"] <= 0.06)).mean())
    return result


def safe_median(values: pd.Series) -> float | None:
    arr = pd.to_numeric(values, errors="coerce").dropna().to_numpy(float)
    return float(np.median(arr)) if len(arr) else None


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Lifecycle Router Replay",
        "",
        f"- Strategy: `{report['strategy']}`",
        f"- Test rows: {report['dense_rows_test']}",
        f"- Test lifecycles: {report['lifecycles_test']}",
        f"- Confirm bars: {report['settings']['confirm_bars']}",
        f"- Cooldown bars: {report['settings']['cooldown_bars']}",
        f"- High-pump enabled: {report['settings']['high_pump_enabled']}",
        f"- Slow short gate: {report['settings']['slow_short_gate']}",
        "",
        "## Signal Summary",
        "",
    ]
    lines.extend(render_summary_table(report["signals_first_per_lifecycle_level"]))
    lines += ["", "## High Pump Only", ""]
    lines.extend(render_summary_table(report["signals_high_pump_only"]))
    lines += ["", "## Router Only", ""]
    lines.extend(render_summary_table(report["signals_router_only"]))
    lines += ["", "## All Signals", ""]
    lines.extend(render_summary_table(report["signals_all"]))
    lines += ["", "## Route Counts", "", "```json", json.dumps(report["route_counts"], ensure_ascii=False, indent=2), "```"]
    lines += ["", "## State Counts", "", "```json", json.dumps(report["state_counts"], ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines)


def render_summary_table(summary: dict[str, Any]) -> list[str]:
    if not summary or summary.get("total", 0) == 0:
        return ["No signals."]
    rows = []
    for key, value in summary.items():
        if key == "total" or not isinstance(value, dict) or value.get("signals", 0) == 0:
            continue
        rows.append(
            [
                key,
                value.get("signals", 0),
                pct(value.get("median_up_24h")),
                pct(value.get("median_drop_6h")),
                pct(value.get("median_drop_24h")),
                pct(value.get("median_drop_72h")),
                pct(value.get("median_short_adverse_24h")),
                pct(value.get("clean_short_24h_rate")),
            ]
        )
    out = [
        f"Total signals: {summary.get('total', 0)}",
        "",
        "| Segment | Signals | Med Up24 | Med Drop6 | Med Drop24 | Med Drop72 | Med Short Adv24 | Clean Short24 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    out.extend("| " + " | ".join(str(x) for x in row) + " |" for row in rows)
    return out


def pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "-"


if __name__ == "__main__":
    raise SystemExit(main())
