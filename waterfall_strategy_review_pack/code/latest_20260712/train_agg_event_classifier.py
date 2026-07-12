"""Train aggTrade event classifiers for true waterfall vs fake breaks.

The dataset is produced by build_agg_event_features.py.  Each row is an event
with closed-1m context plus aggTrade features available by several cutoffs
inside the signal minute.  This script uses a time split, not random split.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


META = {
    "symbol",
    "label",
    "family",
    "event_time",
    "event_iso",
    "event_price",
}

CURRENT_1M_CONTEXT_FEATURES = [
    "ret_30m",
    "ret_2h",
    "ret_4h",
    "ret_12h",
    "ret_24h",
    "runup_24h",
    "dd_from_24h_high",
    "qv30",
    "volr20",
    "volr5_20",
    "tsell",
    "body_drop",
    "drop_2m",
    "drop_5m",
    "close_pos",
    "range_pct",
    "break_depth",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(args.features)
    df = df[df["label"].isin(["true_waterfall", "fake_break"])].copy()
    df["event_dt"] = pd.to_datetime(df["event_time"], unit="ms", utc=True)
    df["y"] = (df["label"] == "true_waterfall").astype(int)
    rows: list[dict[str, Any]] = []
    scored_parts: list[pd.DataFrame] = []
    for cutoff in [int(x) for x in args.cutoffs.split(",") if x.strip()]:
        result, scored = train_cutoff(df.copy(), cutoff, args)
        rows.extend(result)
        if not scored.empty:
            scored_parts.append(scored)
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(["period", "score_metric"], ascending=[True, False])
    scored_all = pd.concat(scored_parts, ignore_index=True) if scored_parts else pd.DataFrame()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = out_dir / f"agg_event_classifier_summary_{stamp}.csv"
    scored_path = out_dir / f"agg_event_classifier_scored_{stamp}.csv"
    report_path = out_dir / f"agg_event_classifier_report_{stamp}.md"
    summary.to_csv(summary_path, index=False)
    if not scored_all.empty:
        scored_all.to_csv(scored_path, index=False)
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "scored": str(scored_path), "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0


def train_cutoff(df: pd.DataFrame, cutoff: int, args: argparse.Namespace) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    features = feature_columns(df, cutoff, args.mode)
    data = df.replace([np.inf, -np.inf], np.nan).dropna(subset=features + ["y"]).copy()
    train = data[data["event_dt"] < pd.Timestamp(args.validation_start, tz="UTC")].copy()
    val = data[data["event_dt"] >= pd.Timestamp(args.validation_start, tz="UTC")].copy()
    if len(train) < args.min_train or len(val) < args.min_val or train["y"].nunique() < 2 or val["y"].nunique() < 2:
        return [], pd.DataFrame()
    x_train = make_x(train, features, args.mode)
    x_val = make_x(val, features, args.mode).reindex(columns=x_train.columns, fill_value=0.0)
    model = lgb.LGBMClassifier(
        n_estimators=args.n_estimators,
        learning_rate=0.035,
        num_leaves=31,
        min_child_samples=60,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=3.0,
        random_state=17 + cutoff,
        n_jobs=args.n_jobs,
        verbose=-1,
    )
    model.fit(x_train, train["y"])
    train["score"] = model.predict_proba(x_train)[:, 1]
    val["score"] = model.predict_proba(x_val)[:, 1]
    train_auc = safe_auc(train["y"], train["score"])
    val_auc = safe_auc(val["y"], val["score"])
    rows: list[dict[str, Any]] = []
    for q in [float(x) for x in args.quantiles.split(",") if x.strip()]:
        threshold = float(train["score"].quantile(q))
        for period, part, auc in (
            ("train", train[train["score"] >= threshold], train_auc),
            ("validation", val[val["score"] >= threshold], val_auc),
        ):
            if len(part) < args.min_selected:
                continue
            row = metrics(part)
            row.update(
                {
                    "period": period,
                    "cutoff_sec": cutoff,
                    "quantile": q,
                    "threshold": threshold,
                    "mode": args.mode,
                    "auc": auc,
                    "train_rows": len(train),
                    "validation_rows": len(val),
                    "train_pos_rate": float(train["y"].mean()),
                    "validation_pos_rate": float(val["y"].mean()),
                    "features": len(features),
                }
            )
            row["score_metric"] = score_row(row)
            rows.append(row)
    for period, part, auc in (("base_train", train, train_auc), ("base_validation", val, val_auc)):
        row = metrics(part)
        row.update(
            {
                "period": period,
                "cutoff_sec": cutoff,
                "quantile": 0.0,
                "threshold": 0.0,
                "mode": args.mode,
                "auc": auc,
                "train_rows": len(train),
                "validation_rows": len(val),
                "train_pos_rate": float(train["y"].mean()),
                "validation_pos_rate": float(val["y"].mean()),
                "features": len(features),
            }
        )
        row["score_metric"] = score_row(row)
        rows.append(row)
    scored = pd.concat([train, val], ignore_index=True)
    scored["cutoff_sec"] = cutoff
    scored["mode"] = args.mode
    return rows, scored


def feature_columns(df: pd.DataFrame, cutoff: int, mode: str) -> list[str]:
    allowed_prefixes = ["pre5m_", "pre2m_", "pre60s_", "pre30s_", "pre10s_", f"m0_{cutoff}s_"]
    features = []
    if mode == "closed_1m":
        features.extend([c for c in CURRENT_1M_CONTEXT_FEATURES if c in df.columns])
    for col in df.columns:
        if col in META or col in {"event_dt", "y"}:
            continue
        if any(col.startswith(prefix) for prefix in allowed_prefixes) and pd.api.types.is_numeric_dtype(df[col]):
            features.append(col)
    return sorted(set(features))


def make_x(df: pd.DataFrame, features: list[str], mode: str) -> pd.DataFrame:
    x = df[features].copy()
    if mode == "closed_1m":
        for family in sorted(df["family"].dropna().unique()):
            x[f"family_{family}"] = (df["family"] == family).astype(int)
    return x


def metrics(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(df)),
        "symbols": int(df["symbol"].nunique()),
        "precision": pct(df["y"].mean()),
        "true_events": int(df["y"].sum()),
        "fake_events": int((df["y"] == 0).sum()),
        "median_future_drop_5m": pct(df["future_drop_5m"].median()),
        "median_future_drop_15m": pct(df["future_drop_15m"].median()),
        "median_future_drop_30m": pct(df["future_drop_30m"].median()),
        "median_adverse_5m": pct(df["adverse_5m"].median()),
        "p80_adverse_5m": pct(df["adverse_5m"].quantile(0.8)),
        "post_pump_share": pct((df["family"] == "post_pump").mean()),
        "downtrend_share": pct((df["family"] == "downtrend_continuation").mean()),
        "other_share": pct((df["family"] == "other").mean()),
    }


def score_row(row: dict[str, Any]) -> float:
    return (
        float(row["precision"]) * 0.06
        + float(row["median_future_drop_15m"]) * 0.12
        - float(row["p80_adverse_5m"]) * 0.08
        + np.log1p(float(row["rows"])) * 0.5
    )


def safe_auc(y: pd.Series, score: pd.Series) -> float:
    try:
        return float(roc_auc_score(y, score))
    except Exception:
        return float("nan")


def pct(value: Any) -> float:
    if value is None or not np.isfinite(value):
        return float("nan")
    return round(float(value) * 100.0, 3)


def render_report(summary: pd.DataFrame) -> str:
    lines = ["# Agg Event Classifier", ""]
    if summary.empty:
        return "# Agg Event Classifier\n\nNo results.\n"
    for period in ("validation", "base_validation", "train", "base_train"):
        part = summary[summary["period"] == period].copy()
        if part.empty:
            continue
        lines.append(f"## {period}")
        lines.append(part.head(80).to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", required=True)
    parser.add_argument("--out-dir", default="backend/storage/ml/agg_event_classifier")
    parser.add_argument("--validation-start", default="2026-04-01")
    parser.add_argument("--cutoffs", default="10,20,30,40,50,59")
    parser.add_argument("--mode", choices=["closed_1m", "preclose_agg"], default="closed_1m")
    parser.add_argument("--quantiles", default="0.50,0.60,0.70,0.80,0.85,0.90,0.93,0.95")
    parser.add_argument("--n-estimators", type=int, default=450)
    parser.add_argument("--n-jobs", type=int, default=4)
    parser.add_argument("--min-train", type=int, default=500)
    parser.add_argument("--min-val", type=int, default=200)
    parser.add_argument("--min-selected", type=int, default=50)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
