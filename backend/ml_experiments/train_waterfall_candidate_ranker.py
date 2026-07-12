"""Train simple LGB rank filters over broad 1m waterfall candidates.

This is for research only. It tests whether past-only 1m features can rank
which broad waterfall triggers are worth shorting, especially range breakdowns
where hand-written rules failed on recent data.
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


META = {
    "symbol", "signal_time", "entry_time", "exit_time", "signal_time_iso", "entry_time_iso", "exit_time_iso",
    "entry", "exit", "ret", "mae", "mfe", "hold_min", "exit_reason",
}


FEATURES = [
    "ret_30m", "ret_2h", "ret_4h", "ret_12h", "ret_24h", "runup_24h", "dd_from_24h_high",
    "qv30", "volr20", "volr5_20", "tsell", "tsell5", "body_drop", "drop_2m", "drop_5m",
    "close_pos", "upper_wick", "lower_wick", "range_pct", "break_depth", "red_streak",
    "qv_over_prev6max",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(args.trades)
    if args.rules and args.rules.strip().lower() != "all":
        keep_rules = {x.strip() for x in args.rules.split(",") if x.strip()}
        df = df[df["rule"].isin(keep_rules)].copy()
    df["entry_dt"] = pd.to_datetime(df["entry_time"], unit="ms")
    rows: list[dict[str, Any]] = []
    scored_parts: list[pd.DataFrame] = []
    for family in [x.strip() for x in args.families.split(",") if x.strip()]:
        base = df[df["family"] == family].copy()
        if len(base) < args.min_rows:
            continue
        result, scored = train_family(base, family, args)
        rows.extend(result)
        scored_parts.append(scored)
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(["period", "profit_factor", "avg_ret"], ascending=[True, False, False])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = out_dir / f"waterfall_candidate_ranker_summary_{stamp}.csv"
    report_path = out_dir / f"waterfall_candidate_ranker_report_{stamp}.md"
    scored_path = out_dir / f"waterfall_candidate_ranker_scored_{stamp}.csv"
    summary.to_csv(summary_path, index=False)
    if scored_parts:
        pd.concat(scored_parts, ignore_index=True).to_csv(scored_path, index=False)
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "report": str(report_path), "scored": str(scored_path)}, ensure_ascii=False), flush=True)
    return 0


def train_family(base: pd.DataFrame, family: str, args: argparse.Namespace) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    base = base.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES + ["ret", "mae", "mfe"]).copy()
    train = base[base["entry_dt"] < pd.Timestamp(args.validation_start)].copy()
    val = base[base["entry_dt"] >= pd.Timestamp(args.validation_start)].copy()
    if len(train) < args.min_train or len(val) < args.min_val:
        return [], pd.DataFrame()
    train_y = make_target(train, args.target)
    val_y = make_target(val, args.target)
    x_train = make_x(train)
    x_val = make_x(val)
    x_val = x_val.reindex(columns=x_train.columns, fill_value=0)
    model = lgb.LGBMClassifier(
        n_estimators=args.n_estimators,
        learning_rate=0.035,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=2.0,
        random_state=7,
        n_jobs=2,
        verbose=-1,
    )
    model.fit(x_train, train_y)
    train = train.copy()
    val = val.copy()
    train["ml_score"] = model.predict_proba(x_train)[:, 1]
    val["ml_score"] = model.predict_proba(x_val)[:, 1]
    rows: list[dict[str, Any]] = []
    for q in [float(x) for x in args.quantiles.split(",") if x.strip()]:
        threshold = float(train["ml_score"].quantile(q))
        for period, part in (("train", train[train["ml_score"] >= threshold]), ("validation", val[val["ml_score"] >= threshold])):
            if len(part) < 20:
                continue
            row = metrics(part)
            row.update({
                "family": family,
                "period": period,
                "quantile": q,
                "threshold": threshold,
                "base_train": len(train),
                "base_val": len(val),
                "target": args.target,
            })
            rows.append(row)
    scored = pd.concat([train, val], ignore_index=True)
    scored["family_model"] = family
    return rows, scored


def make_x(df: pd.DataFrame) -> pd.DataFrame:
    x = df[FEATURES].copy()
    for rule in sorted(df["rule"].dropna().unique()):
        x[f"rule_{rule}"] = (df["rule"] == rule).astype(int)
    return x


def make_target(df: pd.DataFrame, name: str) -> pd.Series:
    if name == "positive":
        return (df["ret"] > 0).astype(int)
    if name == "big3_low_mae":
        return ((df["ret"] >= 0.03) & (df["mae"] <= 0.035)).astype(int)
    if name == "quality":
        return (((df["ret"] >= 0.018) & (df["mae"] <= 0.03)) | ((df["ret"] > 0) & (df["mfe"] >= 0.04))).astype(int)
    raise ValueError(f"unknown target: {name}")


def metrics(df: pd.DataFrame) -> dict[str, Any]:
    span_days = max(1.0, (float(df["entry_time"].max()) - float(df["entry_time"].min())) / 86_400_000)
    pos = float(df.loc[df["ret"] > 0, "ret"].sum())
    neg = float(-df.loc[df["ret"] < 0, "ret"].sum())
    return {
        "trades": int(len(df)),
        "per_day": round(float(len(df) / span_days), 3),
        "symbols": int(df["symbol"].nunique()),
        "win_rate": pct((df["ret"] > 0).mean()),
        "avg_ret": pct(df["ret"].mean()),
        "median_ret": pct(df["ret"].median()),
        "profit_factor": round(pos / neg, 3) if neg > 0 else 99.0,
        "median_mae": pct(df["mae"].median()),
        "p80_mae": pct(df["mae"].quantile(0.8)),
        "median_mfe": pct(df["mfe"].median()),
        "p80_mfe": pct(df["mfe"].quantile(0.8)),
        "big3_rate": pct((df["ret"] >= 0.03).mean()),
        "big5_rate": pct((df["ret"] >= 0.05).mean()),
        "stop_rate": pct((df["exit_reason"] == "stop").mean()),
        "median_hold_min": round(float(df["hold_min"].median()), 2),
    }


def pct(value: Any) -> float:
    if value is None or not np.isfinite(value):
        return float("nan")
    return round(float(value) * 100.0, 3)


def render_report(summary: pd.DataFrame) -> str:
    lines = ["# Waterfall Candidate Ranker", ""]
    if summary.empty:
        return "# Waterfall Candidate Ranker\n\nNo results.\n"
    for period in ("validation", "train"):
        part = summary[summary["period"] == period].copy()
        if part.empty:
            continue
        lines.append(f"## {period}")
        lines.append(part.head(80).to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", required=True)
    parser.add_argument("--out-dir", default="backend/storage/ml/waterfall_candidate_ranker")
    parser.add_argument("--families", default="range_breakdown,post_pump,downtrend_continuation,momentum_dump,other")
    parser.add_argument("--rules", default="all_1m_break,wick_reject_1m,strong_sell_1m")
    parser.add_argument("--target", default="quality", choices=["positive", "big3_low_mae", "quality"])
    parser.add_argument("--validation-start", default="2026-04-01")
    parser.add_argument("--quantiles", default="0.70,0.80,0.85,0.90,0.93,0.95,0.97")
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--min-rows", type=int, default=500)
    parser.add_argument("--min-train", type=int, default=300)
    parser.add_argument("--min-val", type=int, default=100)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
