"""Try long-entry models with aggTrade and bookDepth microstructure features.

This is an offline experiment only. It downloads Binance Vision daily public
files for selected symbol-days and derives features strictly up to the closed
candidate candle cutoff. It does not modify production models.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml_experiments import train_long_clean_flow as clean


DAY_MS = 86_400_000
BAR_MS = 15 * 60_000
MIN_MS = 60_000
BASE_URL = "https://data.binance.vision/data/futures/um/daily"

AGG_WINDOWS = {
    "1m": 1 * MIN_MS,
    "3m": 3 * MIN_MS,
    "5m": 5 * MIN_MS,
    "15m": 15 * MIN_MS,
    "30m": 30 * MIN_MS,
}
BOOK_PCTS = (1, 2, 5)


@dataclass(frozen=True)
class ModelResult:
    target: str
    feature_set: str
    rows: int
    train_rows: int
    val_rows: int
    holdout_rows: int
    auc: float | None
    ap: float | None
    thresholds: dict[str, float]
    threshold_metrics: dict[str, dict[str, Any]]
    top_importance: list[dict[str, Any]]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    candidates = load_candidates(Path(args.candidates), args.days, args.top_symbols, args.max_rows, symbols)
    print(f"selected rows={len(candidates)} symbols={candidates['symbol'].nunique()}", flush=True)
    tasks = required_symbol_dates(candidates, lookback_ms=max(AGG_WINDOWS.values()))
    if args.max_symbol_days > 0:
        tasks = tasks[: args.max_symbol_days]
    task_set = set(tasks)
    print(f"symbol-days={len(tasks)}", flush=True)

    if not args.no_download:
        download_tasks(tasks, cache_dir, include_depth=not args.no_depth, sleep_seconds=args.sleep, max_zip_mb=args.max_zip_mb)

    rows = build_micro_dataset(candidates, task_set, cache_dir, include_depth=not args.no_depth)
    rows = rows.replace([np.inf, -np.inf], np.nan)
    rows = add_extra_targets(rows)
    micro_cols = micro_columns(include_depth=not args.no_depth)
    rows["micro_available"] = rows[[c for c in micro_cols if c in rows.columns]].notna().any(axis=1)
    rows = rows[rows["micro_available"]].sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    if len(rows) < 80:
        raise SystemExit(f"not enough rows with micro features: {len(rows)}")

    dataset_path = out_dir / "long_microstructure_dataset.parquet"
    rows.to_parquet(dataset_path, index=False)

    base_cols = [c for c in clean.mlf.feature_columns() + clean.LONG_EXTRA + clean.PROFILE_COLUMNS + clean.FLOW_COLUMNS if c in rows.columns]
    micro_cols = [c for c in micro_cols if c in rows.columns]
    feature_sets = {
        "base_flow": base_cols,
        "base_flow_micro": base_cols + micro_cols,
        "micro_only": micro_cols,
    }
    targets = ["y_smooth5_24", "y_smooth8_48", "y_clean_48h", "y_old_long_start"]
    results: list[ModelResult] = []
    for target in targets:
        for feature_name, cols in feature_sets.items():
            print(f"training {target}/{feature_name}", flush=True)
            result = train_one(rows, target, feature_name, cols, out_dir)
            if result is not None:
                results.append(result)

    payload = {
        "source_candidates": str(Path(args.candidates)),
        "dataset": str(dataset_path),
        "cache_dir": str(cache_dir),
        "rows": int(len(rows)),
        "symbols": int(rows["symbol"].nunique()),
        "start_time": int(rows["entry_time"].min()),
        "end_time": int(rows["entry_time"].max()),
        "symbol_days": len(tasks),
        "include_depth": not args.no_depth,
        "notes": [
            "aggTrade features use transact_time <= entry_time + 15m candle close.",
            "bookDepth features use latest depth snapshot timestamp <= closed candle cutoff.",
            "This is a sampled experiment, not a production model replacement.",
        ],
        "results": [r.__dict__ for r in results],
    }
    (out_dir / "long_microstructure_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "long_microstructure_results.md").write_text(render_report(payload), encoding="utf-8")
    print(json.dumps({"out": str(out_dir), "rows": len(rows), "symbols": rows["symbol"].nunique()}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Long-entry microstructure experiment.")
    parser.add_argument("--candidates", default="backend/storage/ml/long_clean_flow_v2/long_clean_flow_dataset.parquet")
    parser.add_argument("--out-dir", default="backend/storage/ml/long_microstructure")
    parser.add_argument("--cache-dir", default="backend/storage/ml/binance_vision_micro")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--top-symbols", type=int, default=10)
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--max-symbol-days", type=int, default=140)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-zip-mb", type=float, default=18.0)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--no-depth", action="store_true")
    parser.add_argument("--no-download", action="store_true")
    return parser.parse_args(argv)


def load_candidates(path: Path, days: int, top_symbols: int, max_rows: int, symbols: list[str]) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if days > 0:
        start = int(df["entry_time"].max()) - days * DAY_MS
        df = df[df["entry_time"] >= start].copy()
    if symbols:
        df = df[df["symbol"].isin(symbols)].copy()
    elif top_symbols > 0:
        symbols = df["symbol"].value_counts().head(top_symbols).index
        df = df[df["symbol"].isin(symbols)].copy()
    df = df.sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    if max_rows > 0:
        df = df.tail(max_rows).reset_index(drop=True)
    df["cutoff_time"] = df["entry_time"].astype("int64") + BAR_MS
    return df


def required_symbol_dates(rows: pd.DataFrame, lookback_ms: int) -> list[tuple[str, str]]:
    tasks: set[tuple[str, str]] = set()
    for row in rows[["symbol", "cutoff_time"]].itertuples(index=False):
        start = int(row.cutoff_time) - lookback_ms
        end = int(row.cutoff_time)
        for ts in (start, end):
            date = pd.to_datetime(ts, unit="ms", utc=True).strftime("%Y-%m-%d")
            tasks.add((str(row.symbol), date))
    return sorted(tasks, key=lambda x: (x[1], x[0]))


def download_tasks(tasks: list[tuple[str, str]], cache_dir: Path, include_depth: bool, sleep_seconds: float, max_zip_mb: float) -> None:
    total = len(tasks)
    for i, (symbol, date) in enumerate(tasks, 1):
        ensure_file("aggTrades", symbol, date, cache_dir, max_zip_mb=max_zip_mb)
        if include_depth:
            ensure_file("bookDepth", symbol, date, cache_dir, max_zip_mb=max_zip_mb)
        if i % 25 == 0:
            print(f"download checked {i}/{total}", flush=True)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def ensure_file(dataset: str, symbol: str, date: str, cache_dir: Path, max_zip_mb: float) -> Path | None:
    filename = f"{symbol}-{dataset}-{date}.zip"
    path = cache_dir / dataset / symbol / filename
    missing = path.with_suffix(path.suffix + ".missing")
    if path.exists():
        return path
    if missing.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE_URL}/{dataset}/{symbol}/{filename}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=15) as head:
            size = int(head.headers.get("Content-Length") or "0")
        if max_zip_mb > 0 and size > max_zip_mb * 1024 * 1024:
            missing.write_text(f"too_large {size} {url}", encoding="utf-8")
            print(f"download skip too large {dataset}/{symbol}/{date}: {size / 1024 / 1024:.1f}MB", flush=True)
            return None
        with urllib.request.urlopen(url, timeout=45) as response:
            tmp.write_bytes(response.read())
        tmp.replace(path)
        return path
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            missing.write_text(url, encoding="utf-8")
            return None
        raise
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        print(f"download skip {dataset}/{symbol}/{date}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return None


def build_micro_dataset(rows: pd.DataFrame, task_set: set[tuple[str, str]], cache_dir: Path, include_depth: bool) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for symbol, group in rows.groupby("symbol", sort=False):
        dates = sorted({d for s, d in task_set if s == symbol})
        if not dates:
            continue
        agg = read_agg_days(cache_dir, symbol, dates)
        depth = read_depth_days(cache_dir, symbol, dates) if include_depth else pd.DataFrame()
        if agg.empty and depth.empty:
            continue
        features = []
        for row in group.itertuples(index=False):
            features.append(micro_features_for_cutoff(agg, depth, int(row.cutoff_time), include_depth=include_depth))
        feat = pd.DataFrame(features, index=group.index)
        parts.append(pd.concat([group.reset_index(drop=True), feat.reset_index(drop=True)], axis=1))
        if len(parts) % 5 == 0:
            print(f"micro built symbols={len(parts)} rows={sum(len(x) for x in parts)}", flush=True)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def read_agg_days(cache_dir: Path, symbol: str, dates: list[str]) -> pd.DataFrame:
    frames = []
    for date in dates:
        path = cache_dir / "aggTrades" / symbol / f"{symbol}-aggTrades-{date}.zip"
        if not path.exists():
            continue
        try:
            with zipfile.ZipFile(path) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    df = pd.read_csv(f)
        except Exception:
            continue
        if df.empty:
            continue
        df["quote"] = pd.to_numeric(df["price"], errors="coerce") * pd.to_numeric(df["quantity"], errors="coerce")
        df["ts"] = pd.to_numeric(df["transact_time"], errors="coerce").astype("Int64")
        df["buyer_taker"] = ~df["is_buyer_maker"].astype(bool)
        frames.append(df[["ts", "price", "quantity", "quote", "buyer_taker"]])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).dropna(subset=["ts", "quote"]).sort_values("ts")
    out["ts"] = out["ts"].astype("int64")
    return out


def read_depth_days(cache_dir: Path, symbol: str, dates: list[str]) -> pd.DataFrame:
    frames = []
    for date in dates:
        path = cache_dir / "bookDepth" / symbol / f"{symbol}-bookDepth-{date}.zip"
        if not path.exists():
            continue
        try:
            with zipfile.ZipFile(path) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    df = pd.read_csv(f)
        except Exception:
            continue
        if df.empty:
            continue
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df["ts"] = (ts.astype("int64") // 1_000_000).where(ts.notna())
        df["percentage"] = pd.to_numeric(df["percentage"], errors="coerce")
        df["notional"] = pd.to_numeric(df["notional"], errors="coerce")
        frames.append(df[["ts", "percentage", "notional"]])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).dropna(subset=["ts", "percentage", "notional"]).sort_values("ts")
    out["ts"] = out["ts"].astype("int64")
    return out


def micro_features_for_cutoff(agg: pd.DataFrame, depth: pd.DataFrame, cutoff: int, include_depth: bool) -> dict[str, float]:
    out: dict[str, float] = {}
    for label, window_ms in AGG_WINDOWS.items():
        part = agg[(agg["ts"] > cutoff - window_ms) & (agg["ts"] <= cutoff)] if not agg.empty else pd.DataFrame()
        prefix = f"agg_{label}"
        if part.empty:
            fill_agg_empty(out, prefix)
            continue
        quote = part["quote"].astype(float)
        buy_quote = quote.where(part["buyer_taker"].astype(bool), 0.0)
        sell_quote = quote.where(~part["buyer_taker"].astype(bool), 0.0)
        total = float(quote.sum())
        buy = float(buy_quote.sum())
        sell = float(sell_quote.sum())
        prices = pd.to_numeric(part["price"], errors="coerce")
        out[f"{prefix}_quote"] = total
        out[f"{prefix}_count"] = float(len(part))
        out[f"{prefix}_buy_ratio"] = safe_div(buy, total, np.nan)
        out[f"{prefix}_net_buy"] = safe_div(buy - sell, total, np.nan)
        out[f"{prefix}_avg_quote"] = safe_div(total, len(part), np.nan)
        out[f"{prefix}_max_quote_share"] = safe_div(float(quote.max()), total, np.nan)
        out[f"{prefix}_top5_quote_share"] = safe_div(float(quote.nlargest(max(1, math.ceil(len(quote) * 0.05))).sum()), total, np.nan)
        out[f"{prefix}_price_ret"] = safe_div(float(prices.iloc[-1]), float(prices.iloc[0]), np.nan) - 1.0 if len(prices) else np.nan
    out["agg_quote_accel_3m_15m"] = safe_div(out.get("agg_3m_quote"), out.get("agg_15m_quote", np.nan) / 5.0, np.nan)
    out["agg_quote_accel_5m_30m"] = safe_div(out.get("agg_5m_quote"), out.get("agg_30m_quote", np.nan) / 6.0, np.nan)
    out["agg_buy_ratio_delta_3m_15m"] = out.get("agg_3m_buy_ratio", np.nan) - out.get("agg_15m_buy_ratio", np.nan)
    out["agg_net_buy_delta_3m_15m"] = out.get("agg_3m_net_buy", np.nan) - out.get("agg_15m_net_buy", np.nan)
    out["agg_burst_1m_15m"] = safe_div(out.get("agg_1m_quote"), out.get("agg_15m_quote", np.nan) / 15.0, np.nan)
    if include_depth:
        out.update(depth_features(depth, cutoff))
    return out


def fill_agg_empty(out: dict[str, float], prefix: str) -> None:
    for suffix in ("quote", "count", "buy_ratio", "net_buy", "avg_quote", "max_quote_share", "top5_quote_share", "price_ret"):
        out[f"{prefix}_{suffix}"] = np.nan


def depth_features(depth: pd.DataFrame, cutoff: int) -> dict[str, float]:
    out: dict[str, float] = {}
    if depth.empty:
        return {name: np.nan for name in depth_columns()}
    eligible = depth[depth["ts"] <= cutoff]
    if eligible.empty:
        return {name: np.nan for name in depth_columns()}
    ts = int(eligible["ts"].iloc[-1])
    snap = eligible[eligible["ts"] == ts]
    out["book_age_sec"] = (cutoff - ts) / 1000.0
    for pct in BOOK_PCTS:
        bid = pct_notional(snap, -pct)
        ask = pct_notional(snap, pct)
        out[f"book_bid_{pct}pct"] = bid
        out[f"book_ask_{pct}pct"] = ask
        out[f"book_imb_{pct}pct"] = safe_div(bid - ask, bid + ask, np.nan)
        out[f"book_total_{pct}pct"] = bid + ask if np.isfinite(bid) and np.isfinite(ask) else np.nan
    out["book_bid_slope_1_5"] = safe_div(out.get("book_bid_1pct"), out.get("book_bid_5pct"), np.nan)
    out["book_ask_slope_1_5"] = safe_div(out.get("book_ask_1pct"), out.get("book_ask_5pct"), np.nan)
    return out


def pct_notional(snap: pd.DataFrame, pct: int) -> float:
    exact = snap[np.isclose(snap["percentage"].astype(float), float(pct))]
    if exact.empty:
        return np.nan
    return float(exact["notional"].iloc[0])


def micro_columns(include_depth: bool) -> list[str]:
    cols: list[str] = []
    for label in AGG_WINDOWS:
        prefix = f"agg_{label}"
        cols.extend(
            [
                f"{prefix}_quote",
                f"{prefix}_count",
                f"{prefix}_buy_ratio",
                f"{prefix}_net_buy",
                f"{prefix}_avg_quote",
                f"{prefix}_max_quote_share",
                f"{prefix}_top5_quote_share",
                f"{prefix}_price_ret",
            ]
        )
    cols.extend(["agg_quote_accel_3m_15m", "agg_quote_accel_5m_30m", "agg_buy_ratio_delta_3m_15m", "agg_net_buy_delta_3m_15m", "agg_burst_1m_15m"])
    if include_depth:
        cols.extend(depth_columns())
    return cols


def depth_columns() -> list[str]:
    cols = ["book_age_sec"]
    for pct in BOOK_PCTS:
        cols.extend([f"book_bid_{pct}pct", f"book_ask_{pct}pct", f"book_imb_{pct}pct", f"book_total_{pct}pct"])
    cols.extend(["book_bid_slope_1_5", "book_ask_slope_1_5"])
    return cols


def add_extra_targets(rows: pd.DataFrame) -> pd.DataFrame:
    rows = rows.copy()
    rows["y_smooth5_24"] = (
        (rows["future_high_24h"] >= 0.05)
        & (rows["adverse_before_up5"] <= 0.025)
        & (rows["first_2h_adverse"] <= 0.018)
        & (rows["first_6h_adverse"] <= 0.030)
    ).astype("int8")
    rows["y_smooth8_48"] = (
        (rows["future_high_48h"] >= 0.08)
        & (rows["adverse_before_up5"] <= 0.030)
        & (rows["first_2h_adverse"] <= 0.022)
        & (rows["first_6h_adverse"] <= 0.035)
        & (rows["first_24h_adverse"] <= 0.055)
    ).astype("int8")
    return rows


def train_one(rows: pd.DataFrame, target: str, feature_set: str, cols: list[str], out_dir: Path) -> ModelResult | None:
    work = rows.dropna(subset=[target]).copy()
    split = split_masks(work)
    train = work[split["train"]]
    val = work[split["val"]]
    hold = work[split["holdout"]]
    min_split = 8 if len(work) < 240 else 20
    if min(len(train), len(val), len(hold)) < min_split:
        print(f"skip {target}/{feature_set}: split too small", flush=True)
        return None
    if train[target].nunique() < 2 or hold[target].nunique() < 2:
        print(f"skip {target}/{feature_set}: one class", flush=True)
        return None
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=450,
        learning_rate=0.035,
        num_leaves=21,
        min_child_samples=12,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_lambda=2.0,
        random_state=42,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(train[cols], train[target].astype(int))
    val_score = model.predict_proba(val[cols])[:, 1]
    hold_score = model.predict_proba(hold[cols])[:, 1]
    thresholds = {f"q{int(q * 100)}": float(np.quantile(val_score, q)) for q in (0.80, 0.85, 0.90, 0.95)}
    metrics = {name: threshold_metrics(hold, hold_score, target, thr) for name, thr in thresholds.items()}
    y = hold[target].astype(int).to_numpy()
    auc = safe_metric(roc_auc_score, y, hold_score)
    ap = safe_metric(average_precision_score, y, hold_score)
    importances = sorted(
        ({"feature": c, "importance": int(v)} for c, v in zip(cols, model.feature_importances_)),
        key=lambda x: x["importance"],
        reverse=True,
    )[:30]
    model.booster_.save_model(str(out_dir / f"{target}_{feature_set}.txt"))
    return ModelResult(
        target=target,
        feature_set=feature_set,
        rows=int(len(work)),
        train_rows=int(len(train)),
        val_rows=int(len(val)),
        holdout_rows=int(len(hold)),
        auc=auc,
        ap=ap,
        thresholds=thresholds,
        threshold_metrics=metrics,
        top_importance=importances,
    )


def split_masks(rows: pd.DataFrame) -> dict[str, pd.Series]:
    times = np.sort(rows["entry_time"].unique())
    q60, q80 = np.quantile(times, [0.50, 0.75] if len(times) < 240 else [0.60, 0.80])
    embargo = 0 if len(times) < 240 else DAY_MS
    t = rows["entry_time"]
    return {
        "train": t < q60,
        "val": (t >= q60 + embargo) & (t <= q80),
        "holdout": t >= q80 + embargo,
    }


def threshold_metrics(rows: pd.DataFrame, score: np.ndarray, target: str, threshold: float) -> dict[str, Any]:
    selected = rows[score >= threshold]
    if selected.empty:
        return {"signals": 0}
    return {
        "signals": int(len(selected)),
        "precision": mean(selected[target]),
        "smooth5_24_rate": mean(selected["y_smooth5_24"]),
        "smooth8_48_rate": mean(selected["y_smooth8_48"]),
        "clean_48h_rate": mean(selected["y_clean_48h"]),
        "old_long_start_rate": mean(selected["y_old_long_start"]),
        "median_future_high_24h": median(selected["future_high_24h"]),
        "median_future_high_48h": median(selected["future_high_48h"]),
        "median_future_high_72h": median(selected["future_high_72h"]),
        "median_adverse_before_up5": median(selected["adverse_before_up5"]),
        "median_first_2h_adverse": median(selected["first_2h_adverse"]),
        "median_first_6h_adverse": median(selected["first_6h_adverse"]),
        "median_first_24h_adverse": median(selected["first_24h_adverse"]),
        "symbols": int(selected["symbol"].nunique()),
        "profiles": {str(k): int(v) for k, v in selected["candidate_profile"].value_counts().sort_index().items()},
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Long Microstructure Experiment",
        "",
        f"- Rows: {payload['rows']}",
        f"- Symbols: {payload['symbols']}",
        f"- Symbol-days: {payload['symbol_days']}",
        f"- Include depth: {payload['include_depth']}",
        "",
        "## Results",
        "",
        "| target/features | holdout | AUC | AP | q90 sig | q90 precision | q90 fut48 | q90 advUp5 | q90 first6h | q95 sig | q95 precision | q95 fut48 | q95 advUp5 | q95 first6h |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in payload["results"]:
        q90 = result["threshold_metrics"].get("q90", {})
        q95 = result["threshold_metrics"].get("q95", {})
        lines.append(
            "| {name} | {holdout} | {auc} | {ap} | {q90n} | {q90p} | {q90f} | {q90a} | {q90a6} | {q95n} | {q95p} | {q95f} | {q95a} | {q95a6} |".format(
                name=f"{result['target']}/{result['feature_set']}",
                holdout=result["holdout_rows"],
                auc=fmt_num(result["auc"]),
                ap=fmt_num(result["ap"]),
                q90n=q90.get("signals", 0),
                q90p=fmt_pct(q90.get("precision")),
                q90f=fmt_pct(q90.get("median_future_high_48h")),
                q90a=fmt_pct(q90.get("median_adverse_before_up5")),
                q90a6=fmt_pct(q90.get("median_first_6h_adverse")),
                q95n=q95.get("signals", 0),
                q95p=fmt_pct(q95.get("precision")),
                q95f=fmt_pct(q95.get("median_future_high_48h")),
                q95a=fmt_pct(q95.get("median_adverse_before_up5")),
                q95a6=fmt_pct(q95.get("median_first_6h_adverse")),
            )
        )
    lines += ["", "## Top Micro Importances", ""]
    micro = set(micro_columns(include_depth=bool(payload["include_depth"])))
    for result in payload["results"]:
        tops = [x for x in result["top_importance"] if x["feature"] in micro][:12]
        if not tops:
            continue
        lines += [f"### {result['target']}/{result['feature_set']}", ""]
        for item in tops:
            lines.append(f"- `{item['feature']}`: {item['importance']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def safe_div(a: Any, b: Any, default: float) -> float:
    try:
        a = float(a)
        b = float(b)
        if not np.isfinite(a) or not np.isfinite(b) or b == 0:
            return default
        return a / b
    except Exception:
        return default


def safe_metric(fn: Any, y: np.ndarray, score: np.ndarray) -> float | None:
    try:
        if len(np.unique(y)) < 2:
            return None
        value = float(fn(y, score))
        return value if np.isfinite(value) else None
    except Exception:
        return None


def mean(values: Any) -> float | None:
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    return float(s.mean()) if len(s) else None


def median(values: Any) -> float | None:
    s = pd.to_numeric(pd.Series(values), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return float(s.median()) if len(s) else None


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
