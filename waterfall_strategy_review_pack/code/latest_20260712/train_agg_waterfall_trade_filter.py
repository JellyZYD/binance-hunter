"""Train/search aggTrade filters on executable waterfall trades.

The target is not "did price eventually fall" in isolation.  The target is an
actual short trade outcome: higher PF, lower adverse movement, and enough
frequency.  This makes the agg experiment comparable with the 1m baseline.
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
    "rule",
    "family",
    "signal_time",
    "signal_iso",
    "entry_time",
    "exit_time",
    "entry",
    "exit",
    "ret",
    "mae",
    "mfe",
    "hold_min",
    "exit_reason",
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(args.features)
    df["signal_dt"] = pd.to_datetime(df["signal_time"], unit="ms", utc=True)
    df["y"] = ((df["ret"] >= args.target_ret) & (df["mae"] <= args.max_good_mae)).astype(int)
    rows: list[dict[str, Any]] = []
    scored_parts: list[pd.DataFrame] = []
    for mode in [x.strip() for x in args.modes.split(",") if x.strip()]:
        for cutoff in [int(x) for x in args.cutoffs.split(",") if x.strip()]:
            result, scored = train_one(df.copy(), mode, cutoff, args)
            rows.extend(result)
            if not scored.empty:
                scored_parts.append(scored)
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(["period", "score_metric"], ascending=[True, False])
    scored_all = pd.concat(scored_parts, ignore_index=True) if scored_parts else pd.DataFrame()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = out_dir / f"agg_waterfall_trade_filter_summary_{stamp}.csv"
    scored_path = out_dir / f"agg_waterfall_trade_filter_scored_{stamp}.csv"
    report_path = out_dir / f"agg_waterfall_trade_filter_report_{stamp}.md"
    summary.to_csv(summary_path, index=False)
    if not scored_all.empty:
        scored_all.to_csv(scored_path, index=False)
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "scored": str(scored_path), "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--features", required=True)
    p.add_argument("--out-dir", default="backend/storage/ml/agg_waterfall_trade_filter")
    p.add_argument("--validation-start", default="2026-04-01")
    p.add_argument("--cutoffs", default="10,20,30,40,50,59")
    p.add_argument("--modes", default="closed_1m,preclose_agg")
    p.add_argument("--quantiles", default="0.40,0.50,0.60,0.70,0.80,0.85,0.90,0.93")
    p.add_argument("--target-ret", type=float, default=0.02)
    p.add_argument("--max-good-mae", type=float, default=0.025)
    p.add_argument("--n-estimators", type=int, default=500)
    p.add_argument("--n-jobs", type=int, default=4)
    p.add_argument("--min-train", type=int, default=80)
    p.add_argument("--min-val", type=int, default=30)
    p.add_argument("--min-selected", type=int, default=10)
    return p.parse_args(argv)


def train_one(df: pd.DataFrame, mode: str, cutoff: int, args: argparse.Namespace) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    features = feature_columns(df, mode, cutoff)
    data = df.replace([np.inf, -np.inf], np.nan).dropna(subset=features + ["ret", "mae", "y"]).copy()
    train = data[data["signal_dt"] < pd.Timestamp(args.validation_start, tz="UTC")].copy()
    val = data[data["signal_dt"] >= pd.Timestamp(args.validation_start, tz="UTC")].copy()
    if len(train) < args.min_train or len(val) < args.min_val or train["y"].nunique() < 2 or val["y"].nunique() < 2:
        return [], pd.DataFrame()
    x_train = make_x(train, features, mode)
    x_val = make_x(val, features, mode).reindex(columns=x_train.columns, fill_value=0.0)
    model = lgb.LGBMClassifier(
        n_estimators=args.n_estimators,
        learning_rate=0.035,
        num_leaves=15,
        min_child_samples=20,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=5.0,
        random_state=71 + cutoff,
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
        th = float(train["score"].quantile(q))
        for period, part, auc in (
            ("train", train[train["score"] >= th], train_auc),
            ("validation", val[val["score"] >= th], val_auc),
        ):
            if len(part) < args.min_selected:
                continue
            row = trade_metrics(part)
            row.update(
                {
                    "period": period,
                    "mode": mode,
                    "cutoff_sec": cutoff,
                    "quantile": q,
                    "threshold": th,
                    "auc": auc,
                    "features": len(features),
                    "train_rows": len(train),
                    "validation_rows": len(val),
                    "train_pos_rate": pct(train["y"].mean()),
                    "validation_pos_rate": pct(val["y"].mean()),
                }
            )
            row["score_metric"] = score_row(row)
            rows.append(row)
    for period, part, auc in (("base_train", train, train_auc), ("base_validation", val, val_auc)):
        row = trade_metrics(part)
        row.update(
            {
                "period": period,
                "mode": mode,
                "cutoff_sec": cutoff,
                "quantile": 0.0,
                "threshold": 0.0,
                "auc": auc,
                "features": len(features),
                "train_rows": len(train),
                "validation_rows": len(val),
                "train_pos_rate": pct(train["y"].mean()),
                "validation_pos_rate": pct(val["y"].mean()),
            }
        )
        row["score_metric"] = score_row(row)
        rows.append(row)
    scored = pd.concat([train, val], ignore_index=True)
    scored["mode"] = mode
    scored["cutoff_sec"] = cutoff
    return rows, scored


def feature_columns(df: pd.DataFrame, mode: str, cutoff: int) -> list[str]:
    prefixes = ["pre10m_", "pre5m_", "pre2m_", "pre60s_", "pre30s_", "pre10s_", f"m0_{cutoff}s_"]
    if mode == "closed_1m":
        prefixes.append("full_signal_1m_")
    features: list[str] = []
    for col in df.columns:
        if col in META or col in {"signal_dt", "y"}:
            continue
        if mode == "closed_1m" and col in {
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
            "tsell5",
            "body_drop",
            "drop_2m",
            "drop_5m",
            "close_pos",
            "upper_wick",
            "lower_wick",
            "range_pct",
            "break_depth",
            "red_streak",
            "qv_over_prev6max",
        }:
            features.append(col)
            continue
        if any(col.startswith(prefix) for prefix in prefixes) and pd.api.types.is_numeric_dtype(df[col]):
            features.append(col)
    return sorted(set(features))


def make_x(df: pd.DataFrame, features: list[str], mode: str) -> pd.DataFrame:
    x = df[features].copy()
    if mode == "closed_1m":
        for col in ("family", "rule"):
            for value in sorted(df[col].dropna().unique()):
                x[f"{col}_{value}"] = (df[col] == value).astype(int)
    return x


def trade_metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = df["ret"].astype(float)
    pos = float(ret[ret > 0].sum())
    neg = float(-ret[ret < 0].sum())
    days = max(1, int((df["signal_dt"].max() - df["signal_dt"].min()).days) + 1)
    return {
        "rows": int(len(df)),
        "symbols": int(df["symbol"].nunique()),
        "per_day": round(len(df) / days, 3),
        "win_rate": pct((ret > 0).mean()),
        "avg_ret": pct(ret.mean()),
        "median_ret": pct(ret.median()),
        "pf": round(pos / neg, 3) if neg > 0 else 999.0,
        "median_mae": pct(df["mae"].median()),
        "p80_mae": pct(df["mae"].quantile(0.8)),
        "median_mfe": pct(df["mfe"].median()),
        "big3_rate": pct((ret >= 0.03).mean()),
        "big5_rate": pct((ret >= 0.05).mean()),
        "post_pump_share": pct((df["family"] == "post_pump").mean()),
        "downtrend_share": pct((df["family"] == "downtrend_continuation").mean()),
        "other_share": pct((df["family"] == "other").mean()),
    }


def score_row(row: dict[str, Any]) -> float:
    return (
        float(row["pf"]) * 2.5
        + float(row["avg_ret"]) * 0.20
        + float(row["win_rate"]) * 0.03
        + np.log1p(float(row["rows"])) * 0.35
        - float(row["p80_mae"]) * 0.10
    )


def safe_auc(y: pd.Series, score: pd.Series) -> float:
    try:
        return float(roc_auc_score(y, score))
    except Exception:
        return float("nan")


def pct(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return float("nan")
    if not np.isfinite(out):
        return float("nan")
    return round(out * 100.0, 3)


def render_report(summary: pd.DataFrame) -> str:
    if summary.empty:
        return "# Agg Waterfall Trade Filter\n\nNo results.\n"
    lines = ["# Agg Waterfall Trade Filter", ""]
    for period in ("validation", "base_validation", "train", "base_train"):
        part = summary[summary["period"] == period].copy()
        if part.empty:
            continue
        lines.append(f"## {period}")
        lines.append(part.head(80).to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
