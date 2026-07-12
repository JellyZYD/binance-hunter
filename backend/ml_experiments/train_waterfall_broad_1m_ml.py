"""Train ML filters from broad 1m waterfall candidates.

This script intentionally starts before the hand-written production filters.
It builds a wide candidate set directly from closed 1m candles, simulates a few
exit profiles for every candidate, then checks whether past-only features can
remove fake waterfalls while preserving enough trades.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_experiments.discover_waterfall_patterns import (
    DAY_MS,
    SignalRule,
    classify_family,
    load_symbol,
    parquet_files,
    red_streak,
    simulate_trade,
)


META = {
    "symbol",
    "family",
    "entry_style",
    "exit_profile",
    "signal_time",
    "entry_time",
    "exit_time",
    "signal_time_iso",
    "entry_time_iso",
    "exit_time_iso",
    "entry",
    "exit",
    "ret",
    "mae",
    "mfe",
    "hold_min",
    "exit_reason",
    "target_positive",
    "target_quality",
    "target_big3_low_mae",
}


BASE_FEATURES = [
    "ret_1m",
    "ret_2m",
    "ret_3m",
    "ret_5m",
    "ret_10m",
    "ret_15m",
    "ret_30m",
    "ret_60m",
    "ret_2h",
    "ret_4h",
    "ret_12h",
    "ret_24h",
    "runup_24h",
    "dd_from_24h_high",
    "qv30",
    "qv5",
    "qv10",
    "qv60",
    "qv_ratio_5_30",
    "qv_ratio_10_60",
    "volr20",
    "volr5_20",
    "tsell",
    "tsell5",
    "tsell10",
    "tsell20",
    "body_drop",
    "drop_2m",
    "drop_5m",
    "close_pos",
    "upper_wick",
    "lower_wick",
    "range_pct",
    "break_depth20",
    "break_depth40",
    "ema8_dist",
    "ema21_dist",
    "ema8_21",
    "atr20",
    "red_streak",
    "qv_over_prev6max",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dataset:
        dataset = Path(args.dataset)
    else:
        dataset = build_dataset(args, out_dir)
    if args.build_only:
        return 0

    summary, scored = train_models(dataset, args)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = out_dir / f"waterfall_broad_1m_ml_summary_{stamp}.csv"
    scored_path = out_dir / f"waterfall_broad_1m_ml_scored_{stamp}.csv"
    report_path = out_dir / f"waterfall_broad_1m_ml_report_{stamp}.md"
    summary.to_csv(summary_path, index=False)
    if not scored.empty:
        scored.to_csv(scored_path, index=False)
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"dataset": str(dataset), "summary": str(summary_path), "scored": str(scored_path), "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=os.environ.get("HUNTER_BB_SOURCE", r"E:\A\bb\data"))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--out-dir", default="backend/storage/ml/waterfall_broad_1m_ml")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--cooldown-min", type=int, default=8)
    parser.add_argument("--max-candidates-per-symbol", type=int, default=2500)
    parser.add_argument("--validation-start", default="2026-04-01")
    parser.add_argument("--quantiles", default="0.50,0.60,0.70,0.80,0.85,0.90,0.93,0.95")
    parser.add_argument("--target", default="quality", choices=["positive", "quality", "big3_low_mae"])
    parser.add_argument("--min-rows", type=int, default=600)
    parser.add_argument("--min-train", type=int, default=350)
    parser.add_argument("--min-val", type=int, default=120)
    parser.add_argument("--n-estimators", type=int, default=600)
    parser.add_argument("--n-jobs", type=int, default=4)
    parser.add_argument("--include-global", action="store_true", help="Also train one model per exit profile across all families.")
    return parser.parse_args(argv)


def build_dataset(args: argparse.Namespace, out_dir: Path) -> Path:
    files = parquet_files(Path(args.source), args.max_symbols)
    print(json.dumps({"files": len(files), "days": args.days}, ensure_ascii=False), flush=True)
    rows: list[dict[str, Any]] = []
    profiles = exit_profiles()
    for idx, path in enumerate(files, 1):
        try:
            df = load_symbol(path, args.days)
        except Exception as exc:
            print(f"skip {path.stem}: {exc}", flush=True)
            continue
        if len(df) < 1600:
            continue
        df = add_extra_features(df)
        cand_idx = broad_candidate_indices(df, args.cooldown_min)
        if args.max_candidates_per_symbol and len(cand_idx) > args.max_candidates_per_symbol:
            severity = candidate_severity(df, cand_idx)
            keep = np.argsort(-severity)[: args.max_candidates_per_symbol]
            cand_idx = np.sort(cand_idx[keep])
        symbol = path.stem.upper()
        for i in cand_idx:
            if i < 1500 or i >= len(df) - 390:
                continue
            base = candidate_features(symbol, df, int(i))
            if not base:
                continue
            for profile in profiles:
                trade = simulate_trade(symbol, df, int(i), profile)
                if trade is None:
                    continue
                row = {**base, **asdict(trade)}
                row["exit_profile"] = profile.name
                row["target_positive"] = int(row["ret"] > 0)
                row["target_quality"] = int((row["ret"] >= 0.018 and row["mae"] <= 0.030) or row["ret"] >= 0.035)
                row["target_big3_low_mae"] = int(row["ret"] >= 0.030 and row["mae"] <= 0.035)
                rows.append(row)
        if args.progress_every and idx % args.progress_every == 0:
            print(f"processed {idx}/{len(files)} rows={len(rows)}", flush=True)

    data = pd.DataFrame(rows)
    if not data.empty:
        data["signal_time_iso"] = pd.to_datetime(data["signal_time"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")
        data["entry_time_iso"] = pd.to_datetime(data["entry_time"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")
        data["exit_time_iso"] = pd.to_datetime(data["exit_time"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dataset_path = out_dir / f"waterfall_broad_1m_candidates_{stamp}.csv"
    data.to_csv(dataset_path, index=False)
    summary_path = out_dir / f"waterfall_broad_1m_candidates_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summarize_dataset(data), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"dataset": str(dataset_path), "rows": len(data), "summary": str(summary_path)}, ensure_ascii=False), flush=True)
    return dataset_path


def add_extra_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"].astype(float)
    qv = out["qv"].astype(float)
    tbq = out["tbq"].astype(float)
    for n in (1, 2, 3, 5, 10, 15, 60):
        out[f"ret_{n}m"] = c / c.shift(n) - 1.0
    out["qv5"] = qv.rolling(5).sum()
    out["qv10"] = qv.rolling(10).sum()
    out["qv60"] = qv.rolling(60).sum()
    out["qv_ratio_5_30"] = out["qv5"] / (qv.rolling(30).sum() / 6.0).replace(0, np.nan)
    out["qv_ratio_10_60"] = out["qv10"] / (qv.rolling(60).sum() / 6.0).replace(0, np.nan)
    out["tsell10"] = 1.0 - tbq.rolling(10).sum() / qv.rolling(10).sum().replace(0, np.nan)
    out["tsell20"] = 1.0 - tbq.rolling(20).sum() / qv.rolling(20).sum().replace(0, np.nan)
    out["break_depth20"] = out["prior_body_low_20"] / c - 1.0
    out["break_depth40"] = out["prior_body_low_40"] / c - 1.0
    for lag in range(0, 8):
        out[f"lag{lag}_ret"] = c / c.shift(lag + 1) - 1.0
        for col in ("body_drop", "close_pos", "upper_wick", "lower_wick", "range_pct", "volr20", "tsell"):
            out[f"lag{lag}_{col}"] = out[col].shift(lag)
    return out


def broad_candidate_indices(df: pd.DataFrame, cooldown_min: int) -> np.ndarray:
    close = df["close"].astype(float)
    break20 = close < df["prior_body_low_20"].astype(float) * 0.999
    break40 = close < df["prior_body_low_40"].astype(float) * 0.999
    one_bar = (df["body_drop"] >= 0.0035) & (df["volr20"] >= 1.35)
    two_bar = (df["drop_2m"] >= 0.0060) & (df["volr20"] >= 1.25)
    five_bar = (df["drop_5m"] >= 0.0100) & (df["volr5_20"] >= 1.15)
    mask = (
        (break20 | break40)
        & (one_bar | two_bar | five_bar)
        & (df["qv30"] >= 45_000)
        & (df["close_pos"] <= 0.62)
        & (df["tsell"] >= 0.48)
        & (df["volr20"] <= 10.0)
        & (df["range_pct"] <= 0.16)
    ).fillna(False)
    raw = np.flatnonzero(mask.to_numpy())
    if len(raw) == 0:
        return raw
    kept: list[int] = []
    next_allowed = -1
    for i in raw:
        if i < next_allowed:
            continue
        kept.append(int(i))
        next_allowed = int(i) + max(1, cooldown_min)
    return np.asarray(kept, dtype=int)


def candidate_severity(df: pd.DataFrame, idx: np.ndarray) -> np.ndarray:
    cols = [
        "drop_5m",
        "body_drop",
        "volr20",
        "volr5_20",
        "tsell",
        "break_depth20",
        "break_depth40",
        "qv30",
    ]
    part = df.iloc[idx][cols].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return (
        part["drop_5m"].to_numpy() * 40
        + part["body_drop"].to_numpy() * 30
        + np.log1p(part["volr20"].clip(lower=0).to_numpy()) * 0.8
        + np.log1p(part["volr5_20"].clip(lower=0).to_numpy()) * 0.8
        + part["tsell"].to_numpy()
        + np.maximum(part["break_depth20"].to_numpy(), part["break_depth40"].to_numpy()) * 25
        + np.log1p(part["qv30"].clip(lower=0).to_numpy()) * 0.05
    )


def candidate_features(symbol: str, df: pd.DataFrame, i: int) -> dict[str, Any]:
    row = df.iloc[i]
    family = classify_family(row)
    out: dict[str, Any] = {
        "symbol": symbol,
        "signal_time": int(row["t"]),
        "family": family,
        "entry_style": entry_style(row),
    }
    feature_names = [x for x in BASE_FEATURES if x != "red_streak"] + [
        f"lag{lag}_{col}"
        for lag in range(0, 8)
        for col in ("ret", "body_drop", "close_pos", "upper_wick", "lower_wick", "range_pct", "volr20", "tsell")
    ]
    for col in feature_names:
        value = row.get(col, np.nan)
        if value is None or not np.isfinite(value):
            return {}
        out[col] = float(value)
    out["red_streak"] = int(red_streak(df, i))
    return out


def entry_style(row: pd.Series) -> str:
    if float(row["drop_5m"]) >= 0.030:
        return "deep_5m"
    if float(row["body_drop"]) >= 0.010 and float(row["tsell"]) >= 0.56:
        return "body_sell"
    if float(row["upper_wick"]) >= 0.0025:
        return "wick_reject"
    if float(row["drop_2m"]) >= 0.012:
        return "two_bar"
    return "broad_break"


def exit_profiles() -> list[SignalRule]:
    return [
        profile_rule("tight_fast", stop_cap=0.020, stop_body_high_buffer=0.0025, trail_activate=0.025, trail_rebound=0.008, quick_reclaim_buffer=0.0020, rebound_activate=0.020, rebound_retrace=0.014, max_hold_min=90),
        profile_rule("medium_28_lock", stop_cap=0.028, stop_body_high_buffer=0.0035, trail_activate=0.035, trail_rebound=0.010, quick_reclaim_buffer=0.0030, rebound_activate=0.025, rebound_retrace=0.018, max_hold_min=180),
        profile_rule("let_big_run", stop_cap=0.036, stop_body_high_buffer=0.0045, trail_activate=0.055, trail_rebound=0.018, quick_reclaim_buffer=0.0040, rebound_activate=0.040, rebound_retrace=0.030, max_hold_min=360),
    ]


def profile_rule(name: str, **kwargs: Any) -> SignalRule:
    return SignalRule(
        name=name,
        min_qv30=0,
        min_body_drop=0,
        min_2m_drop=0,
        min_5m_drop=0,
        min_volr20=0,
        min_volr5_20=0,
        min_tsell=0,
        max_close_pos=1,
        min_upper_wick=0,
        break_lookback=20,
        break_buffer=0.001,
        **kwargs,
    )


def train_models(dataset: Path, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = pd.read_csv(dataset)
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    data["entry_dt"] = pd.to_datetime(data["entry_time"], unit="ms")
    features = feature_columns(data)
    rows: list[dict[str, Any]] = []
    scored_parts: list[pd.DataFrame] = []
    for (family, profile), base in data.groupby(["family", "exit_profile"], dropna=False):
        if len(base) < args.min_rows:
            continue
        result, scored = train_one(base.copy(), str(family), str(profile), features, args)
        rows.extend(result)
        if not scored.empty:
            scored_parts.append(scored)
    if args.include_global:
        for profile, base in data.groupby("exit_profile", dropna=False):
            if len(base) < args.min_rows:
                continue
            result, scored = train_one(base.copy(), "all", str(profile), features, args)
            rows.extend(result)
            if not scored.empty:
                scored_parts.append(scored)
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(
            ["period", "family", "score"],
            ascending=[True, True, False],
        )
    scored_all = pd.concat(scored_parts, ignore_index=True) if scored_parts else pd.DataFrame()
    return summary, scored_all


def feature_columns(data: pd.DataFrame) -> list[str]:
    numeric = []
    for col in data.columns:
        if col in META or col == "entry_dt":
            continue
        if pd.api.types.is_numeric_dtype(data[col]):
            numeric.append(col)
    return numeric


def train_one(base: pd.DataFrame, family: str, profile: str, features: list[str], args: argparse.Namespace) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    base = base.replace([np.inf, -np.inf], np.nan).dropna(subset=features + ["ret", "mae", "mfe"]).copy()
    train = base[base["entry_dt"] < pd.Timestamp(args.validation_start)].copy()
    val = base[base["entry_dt"] >= pd.Timestamp(args.validation_start)].copy()
    if len(train) < args.min_train or len(val) < args.min_val:
        return [], pd.DataFrame()
    y_col = f"target_{args.target}"
    x_train = make_x(train, features)
    x_val = make_x(val, features).reindex(columns=x_train.columns, fill_value=0.0)
    model = lgb.LGBMClassifier(
        n_estimators=args.n_estimators,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=3.0,
        random_state=11,
        n_jobs=args.n_jobs,
        verbose=-1,
    )
    model.fit(x_train, train[y_col].astype(int))
    train["ml_score"] = model.predict_proba(x_train)[:, 1]
    val["ml_score"] = model.predict_proba(x_val)[:, 1]
    out_rows: list[dict[str, Any]] = []
    for period, part in (("base_train", train), ("base_validation", val)):
        row = metrics(part)
        row.update(
            {
                "family": family,
                "exit_profile": profile,
                "period": period,
                "quantile": 0.0,
                "threshold": 0.0,
                "base_train": len(train),
                "base_validation": len(val),
                "target": args.target,
            }
        )
        row["score"] = score_row(row)
        out_rows.append(row)
    for q in [float(x) for x in args.quantiles.split(",") if x.strip()]:
        threshold = float(train["ml_score"].quantile(q))
        for period, part in (
            ("train", train[train["ml_score"] >= threshold]),
            ("validation", val[val["ml_score"] >= threshold]),
        ):
            if len(part) < 20:
                continue
            row = metrics(part)
            row.update(
                {
                    "family": family,
                    "exit_profile": profile,
                    "period": period,
                    "quantile": q if not period.startswith("base") else 0.0,
                    "threshold": threshold if not period.startswith("base") else 0.0,
                    "base_train": len(train),
                    "base_validation": len(val),
                    "target": args.target,
                }
            )
            row["score"] = score_row(row)
            out_rows.append(row)
    scored = pd.concat([train, val], ignore_index=True)
    scored["family_model"] = family
    scored["exit_profile_model"] = profile
    return out_rows, scored


def make_x(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    x = df[features].copy()
    if "entry_style" in df.columns:
        for style in sorted(df["entry_style"].dropna().unique()):
            x[f"entry_style_{style}"] = (df["entry_style"] == style).astype(int)
    if "family" in df.columns:
        for family in sorted(df["family"].dropna().unique()):
            x[f"family_{family}"] = (df["family"] == family).astype(int)
    return x


def metrics(df: pd.DataFrame) -> dict[str, Any]:
    span_days = max(1.0, (float(df["entry_time"].max()) - float(df["entry_time"].min())) / DAY_MS)
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
        "quick_reclaim_rate": pct((df["exit_reason"] == "quick_reclaim").mean()),
        "median_hold_min": round(float(df["hold_min"].median()), 2),
    }


def score_row(row: dict[str, Any]) -> float:
    return (
        math.log1p(max(0.0, float(row["profit_factor"]))) * 5.0
        + max(0.0, float(row["avg_ret"])) * 0.45
        + math.log1p(max(0.0, float(row["per_day"]))) * 1.5
        + float(row["win_rate"]) * 0.015
        - float(row["p80_mae"]) * 0.015
    )


def summarize_dataset(data: pd.DataFrame) -> dict[str, Any]:
    if data.empty:
        return {"rows": 0}
    return {
        "rows": int(len(data)),
        "symbols": int(data["symbol"].nunique()),
        "families": {str(k): int(v) for k, v in data.groupby("family").size().to_dict().items()},
        "exit_profiles": {str(k): int(v) for k, v in data.groupby("exit_profile").size().to_dict().items()},
        "base": metrics(data),
        "by_family": {str(k): metrics(g) for k, g in data.groupby("family")},
        "by_family_profile": {f"{fam}/{prof}": metrics(g) for (fam, prof), g in data.groupby(["family", "exit_profile"])},
    }


def render_report(summary: pd.DataFrame) -> str:
    lines = ["# Broad 1m Waterfall ML", ""]
    if summary.empty:
        return "# Broad 1m Waterfall ML\n\nNo results.\n"
    for period in ("validation", "base_validation", "train", "base_train"):
        part = summary[summary["period"] == period].copy()
        if part.empty:
            continue
        lines.append(f"## {period}")
        lines.append(part.head(120).to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


def pct(value: Any) -> float:
    if value is None or not np.isfinite(value):
        return float("nan")
    return round(float(value) * 100.0, 3)


if __name__ == "__main__":
    raise SystemExit(main())
