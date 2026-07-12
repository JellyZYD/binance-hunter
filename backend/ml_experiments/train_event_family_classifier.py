"""Train live-safe pump event family classifiers.

This experiment uses the realized event clusters from cluster_pump_events.py as
historical labels, but builds features only from closed candles available at a
decision time. It is meant to answer whether we can identify the event family
before choosing a long/top/short expert model.

It does not modify production models.

Example:
    python ml_experiments/train_event_family_classifier.py --source "E:\\2C2G\\binance-db"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    confusion_matrix,
    log_loss,
    roc_auc_score,
)

from ml_experiments.train_dump_5m_compare import (
    FEATS,
    Variant,
    aggregate,
    data_end,
    iso_ms,
    parquet_files,
    compute_features_interval,
)
from pump_dump_hunter.ml.train import DAY

VARIANT_15M = Variant("15m", 15 * 60_000, "native")
BAR_MS = 15 * 60_000
HOUR_MS = 3_600_000

FAMILY_MAP = {
    0: "continuation",
    1: "normal_reversal",
    2: "second_distribution",
    3: "fast_dump",
    4: "fast_dump",
    5: "slow_distribution",
}

FAMILY_ORDER = [
    "normal_reversal",
    "slow_distribution",
    "fast_dump",
    "second_distribution",
    "continuation",
]

OPERATIONAL_TARGETS = {
    "fast_dump": {"fast_dump"},
    "slow_or_second_distribution": {"slow_distribution", "second_distribution"},
    "avoid_short_continuation": {"continuation"},
}

CONTEXT_FEATURES = [
    "ctx_bars_since_trigger",
    "ctx_hours_since_trigger",
    "ctx_ret_since_trigger",
    "ctx_high_since_trigger",
    "ctx_low_since_trigger",
    "ctx_drawdown_from_post_trigger_high",
    "ctx_qv_sum_ratio",
    "ctx_qv_recent_ratio",
    "ctx_taker_sell_mean",
    "ctx_red_bar_share",
    "ctx_close_below_trigger",
    "ctx_new_high_after_trigger",
]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=r"E:\2C2G\币安数据库")
    ap.add_argument("--events", default="storage/ml/pump_events_clustered.parquet")
    ap.add_argument("--out", default="storage/ml/event_family_classifier.json")
    ap.add_argument("--report", default="storage/ml/event_family_classifier.md")
    ap.add_argument("--dataset-out", default="storage/ml/event_family_classifier_dataset.parquet")
    ap.add_argument("--model-out", default="storage/ml/event_family_classifier_lgb.txt")
    ap.add_argument("--stage-hours", default="0,2,6,12")
    ap.add_argument("--max-symbols", type=int, default=0)
    args = ap.parse_args(argv)

    source = Path(args.source)
    events_path = Path(args.events)
    if not (source / "klines").is_dir():
        raise SystemExit(f"missing klines directory: {source / 'klines'}")
    if not events_path.exists():
        raise SystemExit(f"missing clustered events: {events_path}")

    stage_hours = parse_stage_hours(args.stage_hours)
    events = load_events(events_path)
    files = {p.stem.upper(): p for p in parquet_files(source, args.max_symbols)}
    rows = build_dataset(events, files, stage_hours)
    if rows.empty:
        raise SystemExit("no dataset rows built")

    dataset_out = Path(args.dataset_out)
    dataset_out.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(dataset_out, index=False)

    result, model = fit_and_evaluate(rows)
    result["source"] = str(source)
    result["events_path"] = str(events_path)
    result["dataset_out"] = str(dataset_out)
    result["stage_hours"] = stage_hours
    result["data_start"] = iso_ms(int(rows.decision_time.min()))
    result["data_end"] = iso_ms(int(rows.decision_time.max()))
    result["rows"] = int(len(rows))
    result["events"] = int(rows.event_id.nunique())
    result["symbols"] = int(rows.symbol.nunique())
    result["family_counts"] = rows.drop_duplicates("event_id").family.value_counts().to_dict()
    result["feature_columns"] = FEATS + CONTEXT_FEATURES
    result["family_order"] = FAMILY_ORDER
    result["operational_targets"] = {k: sorted(v) for k, v in OPERATIONAL_TARGETS.items()}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.report).write_text(render_report(result), encoding="utf-8")
    model.booster_.save_model(args.model_out)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def parse_stage_hours(value: str) -> list[float]:
    out = []
    for part in value.split(","):
        part = part.strip().lower().replace("h", "")
        if not part:
            continue
        out.append(float(part))
    return sorted(set(out))


def load_events(path: Path) -> pd.DataFrame:
    events = pd.read_parquet(path).copy()
    events["family"] = events["cluster"].map(FAMILY_MAP)
    events = events[events["family"].notna()].copy()
    events["event_id"] = (
        events["symbol"].astype(str)
        + "-"
        + events["start_time"].astype("int64").astype(str)
        + "-"
        + events["trigger_time"].astype("int64").astype(str)
    )
    return events.sort_values(["trigger_time", "symbol"]).reset_index(drop=True)


def build_dataset(events: pd.DataFrame, files: dict[str, Path], stage_hours: list[float]) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for i, (symbol, evs) in enumerate(events.groupby("symbol", sort=True), 1):
        path = files.get(str(symbol).upper())
        if path is None:
            continue
        max_stage_ms = int(max(stage_hours) * HOUR_MS) if stage_hours else 0
        start = int(evs.trigger_time.min()) - 7 * DAY
        end = int(evs.trigger_time.max()) + max_stage_ms + DAY
        try:
            g = aggregate(path, start, end, VARIANT_15M)
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)
            continue
        if g is None or len(g) < 200:
            continue
        F = compute_features_interval(g, VARIANT_15M)
        bar_to_ix = {int(v): int(ix) for ix, v in enumerate(g["b"].values)}
        symbol_rows = []
        for ev in evs.itertuples(index=False):
            trig_time = int(ev.trigger_time)
            trig_ix = bar_to_ix.get(trig_time)
            if trig_ix is None:
                continue
            for stage_h in stage_hours:
                decision_time = trig_time + int(stage_h * HOUR_MS)
                if decision_time > int(ev.post_end_time):
                    continue
                ix = bar_to_ix.get(decision_time)
                if ix is None or ix < 96:
                    continue
                feat = F.iloc[ix][FEATS]
                if feat.isna().any():
                    continue
                ctx = context_features(g, trig_ix, ix)
                if any(not np.isfinite(v) for v in ctx.values()):
                    continue
                row = feat.to_dict()
                row.update(ctx)
                row.update(
                    {
                        "symbol": symbol,
                        "event_id": ev.event_id,
                        "cluster": int(ev.cluster),
                        "family": ev.family,
                        "stage": f"t+{stage_h:g}h",
                        "stage_hours": float(stage_h),
                        "trigger_time": trig_time,
                        "decision_time": int(decision_time),
                        "peak_time": int(ev.peak_time),
                        "post_end_time": int(ev.post_end_time),
                    }
                )
                symbol_rows.append(row)
        if symbol_rows:
            rows.append(pd.DataFrame(symbol_rows))
        if i % 25 == 0:
            print(f"loaded {i}/{events.symbol.nunique()} symbols rows={sum(len(x) for x in rows)}", flush=True)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def context_features(g: pd.DataFrame, trig_ix: int, ix: int) -> dict[str, float]:
    close = g["close"].values
    high = g["high"].values
    low = g["low"].values
    open_ = g["open"].values
    qv = g["qv"].values
    tbq = g["tbq"].values
    bars = max(0, ix - trig_ix)
    seg = slice(trig_ix, ix + 1)
    pre = slice(max(0, trig_ix - 96), trig_ix)
    trigger_close = float(close[trig_ix])
    seg_high = float(np.max(high[seg]))
    seg_low = float(np.min(low[seg]))
    pre_qv_mean = float(np.nanmean(qv[pre])) if trig_ix > 0 else float("nan")
    if not np.isfinite(pre_qv_mean) or pre_qv_mean <= 0:
        pre_qv_mean = float(np.nanmean(qv[max(0, ix - 96) : ix + 1]))
    recent_start = max(trig_ix, ix - 15)
    recent_qv_mean = float(np.nanmean(qv[recent_start : ix + 1]))
    tsell = 1.0 - tbq[seg] / np.where(qv[seg] == 0, np.nan, qv[seg])
    bodies = close[seg] - open_[seg]
    return {
        "ctx_bars_since_trigger": float(bars),
        "ctx_hours_since_trigger": float(bars * 0.25),
        "ctx_ret_since_trigger": float(close[ix] / trigger_close - 1.0),
        "ctx_high_since_trigger": float(seg_high / trigger_close - 1.0),
        "ctx_low_since_trigger": float(seg_low / trigger_close - 1.0),
        "ctx_drawdown_from_post_trigger_high": float(close[ix] / seg_high - 1.0),
        "ctx_qv_sum_ratio": float(np.nansum(qv[seg]) / (pre_qv_mean * max(1, bars + 1))),
        "ctx_qv_recent_ratio": float(recent_qv_mean / pre_qv_mean),
        "ctx_taker_sell_mean": float(np.nanmean(tsell)),
        "ctx_red_bar_share": float((bodies < 0).mean()),
        "ctx_close_below_trigger": float(close[ix] < trigger_close),
        "ctx_new_high_after_trigger": float(seg_high > high[trig_ix] * 1.001),
    }


def fit_and_evaluate(df: pd.DataFrame) -> tuple[dict[str, Any], lgb.LGBMClassifier]:
    features = FEATS + CONTEXT_FEATURES
    label_to_id = {label: i for i, label in enumerate(FAMILY_ORDER)}
    y = df["family"].map(label_to_id).astype(int)
    cut = float(np.quantile(df["trigger_time"].drop_duplicates().values, 0.80))
    embargo = 3 * DAY
    train_mask = df["trigger_time"] < cut
    holdout_mask = df["trigger_time"] >= cut + embargo
    train = df[train_mask].copy()
    holdout = df[holdout_mask].copy()
    y_train = train["family"].map(label_to_id).astype(int)
    y_holdout = holdout["family"].map(label_to_id).astype(int)

    params = dict(
        objective="multiclass",
        num_class=len(FAMILY_ORDER),
        n_estimators=450,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=35,
        subsample=0.85,
        colsample_bytree=0.80,
        reg_lambda=2.0,
        class_weight="balanced",
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(train[features], y_train)
    probs = model.predict_proba(holdout[features])
    pred = np.argmax(probs, axis=1)

    result: dict[str, Any] = {
        "split": {
            "cut": iso_ms(int(cut)),
            "embargo_days": 3,
            "train_rows": int(len(train)),
            "holdout_rows": int(len(holdout)),
            "train_events": int(train.event_id.nunique()),
            "holdout_events": int(holdout.event_id.nunique()),
        },
        "multiclass": {
            "accuracy": safe_float(accuracy_score(y_holdout, pred)),
            "balanced_accuracy": safe_float(balanced_accuracy_score(y_holdout, pred)),
            "log_loss": safe_float(log_loss(y_holdout, probs, labels=list(range(len(FAMILY_ORDER))))),
            "confusion": confusion_payload(y_holdout, pred),
            "by_stage": {},
        },
        "one_vs_rest": {},
        "feature_importance": feature_importance(model, features),
    }

    for stage, stage_df in holdout.groupby("stage", sort=True):
        idx = stage_df.index
        yp = y_holdout.loc[idx]
        pp = probs[holdout.index.get_indexer(idx)]
        pr = np.argmax(pp, axis=1)
        result["multiclass"]["by_stage"][stage] = {
            "rows": int(len(stage_df)),
            "events": int(stage_df.event_id.nunique()),
            "accuracy": safe_float(accuracy_score(yp, pr)),
            "balanced_accuracy": safe_float(balanced_accuracy_score(yp, pr)),
            "confusion": confusion_payload(yp, pr),
        }

    for target, families in OPERATIONAL_TARGETS.items():
        target_ids = [label_to_id[x] for x in families]
        score = probs[:, target_ids].sum(axis=1)
        y_binary = holdout["family"].isin(families).astype(int).values
        result["one_vs_rest"][target] = binary_report(holdout, y_binary, score)
    return result, model


def binary_report(holdout: pd.DataFrame, y: np.ndarray, score: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {
        "positives": int(y.sum()),
        "base_rate": safe_float(y.mean()),
        "auc": maybe_auc(y, score),
        "ap": maybe_ap(y, score),
        "thresholds": {},
        "by_stage": {},
    }
    for q in (0.80, 0.90, 0.95, 0.98):
        out["thresholds"][f"q{int(q * 100)}"] = threshold_metrics(y, score, q)
    for stage, stage_df in holdout.groupby("stage", sort=True):
        idx = holdout.index.get_indexer(stage_df.index)
        ys = y[idx]
        ss = score[idx]
        out["by_stage"][stage] = {
            "rows": int(len(stage_df)),
            "events": int(stage_df.event_id.nunique()),
            "positives": int(ys.sum()),
            "base_rate": safe_float(ys.mean()),
            "auc": maybe_auc(ys, ss),
            "q90": threshold_metrics(ys, ss, 0.90),
            "q95": threshold_metrics(ys, ss, 0.95),
        }
    return out


def threshold_metrics(y: np.ndarray, score: np.ndarray, quantile: float) -> dict[str, Any]:
    if len(score) == 0:
        return {"selected": 0, "precision": None, "recall": None, "threshold": None}
    threshold = float(np.quantile(score, quantile))
    selected = score >= threshold
    hits = int((selected & (y == 1)).sum())
    total = int(selected.sum())
    positives = int((y == 1).sum())
    return {
        "threshold": safe_float(threshold),
        "selected": total,
        "precision": safe_float(hits / total) if total else None,
        "recall": safe_float(hits / positives) if positives else None,
    }


def confusion_payload(y_true: pd.Series | np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
    mat = confusion_matrix(y_true, y_pred, labels=list(range(len(FAMILY_ORDER))))
    return {
        "labels": FAMILY_ORDER,
        "matrix": mat.astype(int).tolist(),
    }


def feature_importance(model: lgb.LGBMClassifier, features: list[str]) -> list[dict[str, Any]]:
    gains = model.booster_.feature_importance(importance_type="gain")
    pairs = sorted(zip(features, gains), key=lambda x: x[1], reverse=True)
    return [{"feature": f, "gain": safe_float(g)} for f, g in pairs[:30]]


def maybe_auc(y: np.ndarray, score: np.ndarray) -> float | None:
    if len(np.unique(y)) < 2:
        return None
    return safe_float(roc_auc_score(y, score))


def maybe_ap(y: np.ndarray, score: np.ndarray) -> float | None:
    if len(np.unique(y)) < 2:
        return None
    return safe_float(average_precision_score(y, score))


def safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not np.isfinite(out):
        return None
    return round(out, 6)


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "# Event Family Classifier",
        "",
        "Experiment-only model. Labels come from realized pump-event clusters, but features use only closed candles available at each decision time.",
        "",
        "## Dataset",
        "",
        f"- Rows: {result['rows']}",
        f"- Events: {result['events']}",
        f"- Symbols: {result['symbols']}",
        f"- Data: {result['data_start']} to {result['data_end']}",
        f"- Stages: {', '.join('t+' + str(x).rstrip('0').rstrip('.') + 'h' for x in result['stage_hours'])}",
        f"- Split: train < {result['split']['cut']}, holdout after {result['split']['embargo_days']}d embargo",
        "",
        "## Family Counts",
        "",
        "| Family | Events |",
        "|---|---:|",
    ]
    for family, count in sorted(result["family_counts"].items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| {family} | {count} |")

    mc = result["multiclass"]
    lines += [
        "",
        "## Multiclass Holdout",
        "",
        f"- Accuracy: {pct(mc['accuracy'])}",
        f"- Balanced accuracy: {pct(mc['balanced_accuracy'])}",
        f"- Log loss: {mc['log_loss']}",
        "",
        "| Stage | Rows | Events | Accuracy | Balanced acc |",
        "|---|---:|---:|---:|---:|",
    ]
    for stage, s in mc["by_stage"].items():
        lines.append(f"| {stage} | {s['rows']} | {s['events']} | {pct(s['accuracy'])} | {pct(s['balanced_accuracy'])} |")

    lines += [
        "",
        "## Operational Binary Views",
        "",
        "| Target | Base | AUC | AP | q90 precision | q90 recall | q95 precision | q95 recall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for target, stats in result["one_vs_rest"].items():
        q90 = stats["thresholds"]["q90"]
        q95 = stats["thresholds"]["q95"]
        lines.append(
            f"| {target} | {pct(stats['base_rate'])} | {num(stats['auc'])} | {num(stats['ap'])} | "
            f"{pct(q90['precision'])} | {pct(q90['recall'])} | {pct(q95['precision'])} | {pct(q95['recall'])} |"
        )

    lines += [
        "",
        "## Top Feature Importance",
        "",
        "| Feature | Gain |",
        "|---|---:|",
    ]
    for row in result["feature_importance"][:20]:
        lines.append(f"| {row['feature']} | {num(row['gain'])} |")
    lines += [
        "",
        "## Notes",
        "",
        "- This validates family recognition only. Per-family long/top/short models still need separate training.",
        "- The family label uses future path for historical supervision. No future-path fields are included as model features.",
        "- Small families such as continuation and second_distribution need cautious thresholds or merged operational handling.",
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
    main()
