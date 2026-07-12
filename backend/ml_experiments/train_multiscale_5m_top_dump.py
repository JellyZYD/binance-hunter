"""Train/evaluate 5m multi-scale top and dump experiments.

The production models are not modified. This compares:
- 15m: current production-like feature/label cadence.
- 5m_native: 5m features with native recent raw bars.
- 5m_multi: 5m native features plus last fully closed 15m context features.

Important leakage rule:
For a 5m candle opened at T, the attached 15m context is the latest 15m bar
with open_time <= T - 15m. That means the 15m candle has already closed before
the 5m candle closes.

Example:
    python ml_experiments/train_multiscale_5m_top_dump.py --source "E:\\2C2G\\币安数据库" --days 365
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from ml_experiments.train_dump_5m_compare import (
    BASE_INTERVAL_MS,
    FEATS,
    Variant,
    aggregate,
    bars_15m_units,
    data_end,
    dump_setup_flags_interval,
    fwd,
    iso_ms,
    none_if_nan,
    parquet_files,
    compute_features_interval,
)
from pump_dump_hunter.ml.features import (
    DUMP_CPOS,
    DUMP_DD96,
    LOOKBACK,
    PUMP_RUNUP,
    TOP_CPOS,
    TOP_DD24,
    TOP_UWICK,
)
from pump_dump_hunter.ml.train import DAY

VARIANT_15M = Variant("15m", 15 * 60_000, "native")
VARIANT_5M = Variant("5m_native", 5 * 60_000, "native")
MULTI_PREFIX = "m15_"
M15_FEATS = [f"{MULTI_PREFIX}{c}" for c in FEATS]
TASKS = ("dump", "top")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=r"E:\2C2G\币安数据库")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="storage/ml/multiscale_5m_top_dump.json")
    ap.add_argument("--max-symbols", type=int, default=0)
    args = ap.parse_args(argv)

    source = Path(args.source)
    if not (source / "klines").is_dir():
        raise SystemExit(f"missing klines directory: {source / 'klines'}")

    files = parquet_files(source, args.max_symbols)
    end = data_end(files)
    start = end - args.days * DAY
    result: dict[str, Any] = {
        "source": str(source),
        "days": args.days,
        "data_start": iso_ms(start),
        "data_end": iso_ms(end),
        "symbols_total": len(files),
        "variants": {},
    }

    for name, variant, multiscale in (
        ("15m", VARIANT_15M, False),
        ("5m_native", VARIANT_5M, False),
        ("5m_multi", VARIANT_5M, True),
    ):
        print(f"building {name} from {len(files)} symbols", flush=True)
        df, feats = build_rows(files, start, end, variant, multiscale)
        print(
            f"{name}: rows={len(df)} events={df.event.nunique()} symbols={df.symbol.nunique()} "
            f"top_pos={df.y_top.mean():.4f} dump_pos={df.y_dump.mean():.4f} feats={len(feats)}",
            flush=True,
        )
        result["variants"][name] = {
            "interval": variant.interval_label,
            "multiscale": multiscale,
            "feature_count": len(feats),
            "rows": int(len(df)),
            "events": int(df.event.nunique()),
            "symbols": int(df.symbol.nunique()),
            "tasks": {task: fit_and_evaluate(df, feats, task) for task in TASKS},
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def top_setup_flags_interval(f: pd.DataFrame | pd.Series) -> pd.Series:
    return (
        (f["runup_96"] >= PUMP_RUNUP)
        & (f["dd_24"] >= TOP_DD24)
        & (f["close_pos"] <= TOP_CPOS)
        & (f["uwick"] >= TOP_UWICK)
    )


def build_rows(
    files: list[Path],
    start: int,
    end: int,
    variant: Variant,
    multiscale: bool,
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[pd.DataFrame] = []
    load_start = start - 4 * DAY
    future72 = bars_15m_units(288, variant)
    future24 = bars_15m_units(96, variant)
    hit5_horizon = bars_15m_units(48, variant)
    event_gap = bars_15m_units(48, variant)
    min_rows = bars_15m_units(LOOKBACK + 300, variant)
    feats = FEATS + (M15_FEATS if multiscale else [])

    for i, path in enumerate(files, 1):
        sym = path.stem.upper()
        try:
            g = aggregate(path, load_start, end, variant)
            if multiscale:
                g15 = aggregate(path, load_start, end, VARIANT_15M)
            else:
                g15 = None
        except Exception as exc:
            print(f"{variant.name}: skip {sym}: {exc}", flush=True)
            continue
        if g is None or len(g) < min_rows:
            continue

        F = compute_features_interval(g, variant)
        if multiscale:
            if g15 is None or len(g15) < LOOKBACK + 300:
                continue
            F = attach_closed_15m_context(g, F, g15)

        close = g.close.values
        high = g.high.values
        low = g.low.values
        n = len(g)
        fut72d = close / fwd(pd.Series(low), future72, "min").values - 1
        fut72u = fwd(pd.Series(high), future72, "max").values / close - 1
        fut24u = fwd(pd.Series(high), future24, "max").values / close - 1
        top_setup = top_setup_flags_interval(F).values
        dump_setup = dump_setup_flags_interval(F).values

        valid = np.zeros(n, bool)
        valid[bars_15m_units(LOOKBACK, variant):max(bars_15m_units(LOOKBACK, variant), n - future72 - 1)] = True
        finite = np.isfinite(fut72d) & np.isfinite(fut24u) & F[feats].notna().all(axis=1).values
        cand = (top_setup | dump_setup) & valid & finite
        idx = np.where(cand)[0]
        if len(idx) == 0:
            continue

        reached5 = np.zeros(n, bool)
        adv_before = np.full(n, np.nan)
        time_to_5 = np.full(n, np.nan)
        for ix in idx:
            if not dump_setup[ix]:
                continue
            target = close[ix] * 0.95
            seg_hi = high[ix + 1 : ix + 1 + hit5_horizon]
            seg_low = low[ix + 1 : ix + 1 + hit5_horizon]
            hit = np.where(seg_low <= target)[0]
            if len(hit):
                j = int(hit[0])
                reached5[ix] = True
                adv_before[ix] = seg_hi[: j + 1].max() / close[ix] - 1
                time_to_5[ix] = (j + 1) * (variant.interval_ms / 60_000)

        top_good = top_setup & (F["dd_96"].values >= -0.08) & (fut72d >= 0.15) & (fut24u <= 0.10)
        dump_good = dump_setup & reached5 & (np.nan_to_num(adv_before, nan=1.0) <= 0.08) & (fut72d >= 0.10)

        sub = F.iloc[idx][feats].copy()
        sub["top_setup"] = top_setup[idx].astype("int8")
        sub["dump_setup"] = dump_setup[idx].astype("int8")
        sub["top_good"] = top_good[idx].astype("int8")
        sub["dump_good"] = dump_good[idx].astype("int8")
        sub["reached5"] = reached5[idx].astype("int8")
        sub["adv_before5"] = adv_before[idx]
        sub["time_to_5_minutes"] = time_to_5[idx]
        sub["fut72d"] = fut72d[idx]
        sub["fut72u"] = fut72u[idx]
        sub["fut24u"] = fut24u[idx]
        sub["dd96"] = F["dd_96"].values[idx]
        sub["ts"] = g["b"].values[idx]
        sub["symbol"] = sym
        ev = np.zeros(len(idx), int)
        k = 0
        for m in range(1, len(idx)):
            if idx[m] - idx[m - 1] > event_gap:
                k += 1
            ev[m] = k
        sub["event"] = [f"{sym}-{e}" for e in ev]
        sub["y_top"] = 0
        sub["y_dump"] = 0
        for _, grp in sub.groupby("event", sort=False):
            gt = grp[grp["top_good"] == 1]
            if len(gt):
                sub.loc[gt["ts"].idxmin(), "y_top"] = 1
            gd = grp[grp["dump_good"] == 1]
            if len(gd):
                sub.loc[gd["ts"].idxmin(), "y_dump"] = 1
        rows.append(sub)
        if i % 25 == 0:
            print(f"{variant.name}{'+m15' if multiscale else ''}: loaded {i}/{len(files)} rows={sum(len(x) for x in rows)}", flush=True)

    if not rows:
        return pd.DataFrame(), feats
    return pd.concat(rows, ignore_index=True), feats


def attach_closed_15m_context(g5: pd.DataFrame, f5: pd.DataFrame, g15: pd.DataFrame) -> pd.DataFrame:
    f15 = compute_features_interval(g15, VARIANT_15M).add_prefix(MULTI_PREFIX)
    right = pd.concat([g15[["b"]].reset_index(drop=True), f15.reset_index(drop=True)], axis=1)
    left = pd.DataFrame({"ctx_b": g5["b"].values - BASE_INTERVAL_MS})
    aligned = pd.merge_asof(left, right.sort_values("b"), left_on="ctx_b", right_on="b", direction="backward")
    aligned = aligned[M15_FEATS].reset_index(drop=True)
    return pd.concat([f5.reset_index(drop=True), aligned], axis=1)


def fit_and_evaluate(df: pd.DataFrame, feats: list[str], task: str) -> dict[str, Any]:
    setup_col = f"{task}_setup"
    y_col = f"y_{task}"
    d = df[df[setup_col] == 1].reset_index(drop=True)
    if d.empty:
        return {"error": "empty task dataset"}
    ts = d.ts.values
    cut = int(np.quantile(ts, 0.80))
    train = d[ts < cut].reset_index(drop=True)
    val = d[ts >= cut].reset_index(drop=True)
    pos = int(train[y_col].sum())
    neg = int(len(train) - pos)
    params = dict(
        objective="binary",
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=32,
        min_child_samples=60,
        subsample=0.8,
        colsample_bytree=0.7,
        reg_lambda=1.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        verbosity=-1,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(train[feats], train[y_col])
    train_score = model.predict_proba(train[feats])[:, 1]
    val_score = model.predict_proba(val[feats])[:, 1]
    val = val.assign(score=val_score)
    auc = float("nan")
    ap = float("nan")
    if val[y_col].nunique() > 1:
        auc = float(roc_auc_score(val[y_col], val_score))
        ap = float(average_precision_score(val[y_col], val_score))
    out: dict[str, Any] = {
        "rows": int(len(d)),
        "events": int(d.event.nunique()),
        "train_rows": int(len(train)),
        "val_rows": int(len(val)),
        "positive_rate": float(d[y_col].mean()),
        "val_positive_rate": float(val[y_col].mean()),
        "val_auc": auc,
        "val_ap": ap,
        "train_start": iso_ms(int(train.ts.min())),
        "train_end": iso_ms(int(train.ts.max())),
        "val_start": iso_ms(int(val.ts.min())),
        "val_end": iso_ms(int(val.ts.max())),
        "thresholds": {},
    }
    for q in (0.50, 0.70, 0.80, 0.90, 0.95, 0.98, 0.99):
        threshold = float(np.quantile(train_score, q))
        selected = val[val.score >= threshold].copy()
        selected = selected.sort_values(["symbol", "ts"]).groupby("event", sort=False, as_index=False).first()
        out["thresholds"][f"train_q{int(q * 100)}"] = summarize(selected, val, threshold, task)
    return out


def summarize(selected: pd.DataFrame, val: pd.DataFrame, threshold: float, task: str) -> dict[str, Any]:
    if selected.empty:
        return {"threshold": threshold, "signals": 0}
    period_days = max(1e-9, (int(val.ts.max()) - int(val.ts.min())) / DAY)
    base: dict[str, Any] = {
        "threshold": threshold,
        "signals": int(len(selected)),
        "per_day": float(len(selected) / period_days),
        f"event_precision_y_{task}": float(selected[f"y_{task}"].mean()),
    }
    if task == "dump":
        adv = selected["adv_before5"].replace([np.inf, -np.inf], np.nan)
        t5 = selected["time_to_5_minutes"].replace([np.inf, -np.inf], np.nan)
        base.update(
            {
                "row_good_clean10": float(selected.dump_good.mean()),
                "hit5_12h": float(selected.reached5.mean()),
                "hit10_72h": float((selected.fut72d >= 0.10).mean()),
                "hit15_72h": float((selected.fut72d >= 0.15).mean()),
                "hit20_72h": float((selected.fut72d >= 0.20).mean()),
                "big15_low72": float(((selected.fut72d >= 0.15) & (selected.fut72u <= 0.08)).mean()),
                "mae_before5_median": none_if_nan(adv.median()),
                "mae_before5_p75": none_if_nan(adv.quantile(0.75)),
                "future_up72_median": none_if_nan(selected.fut72u.median()),
                "future_drop72_median": none_if_nan(selected.fut72d.median()),
                "time_to_5_median_minutes": none_if_nan(t5.median()),
                "quick5_2h": float((selected.time_to_5_minutes <= 120).mean()),
            }
        )
    else:
        base.update(
            {
                "row_good_top": float(selected.top_good.mean()),
                "drop15_72h": float((selected.fut72d >= 0.15).mean()),
                "drop20_72h": float((selected.fut72d >= 0.20).mean()),
                "future_up24_le10": float((selected.fut24u <= 0.10).mean()),
                "future_up24_median": none_if_nan(selected.fut24u.median()),
                "future_up24_p75": none_if_nan(selected.fut24u.quantile(0.75)),
                "future_up72_median": none_if_nan(selected.fut72u.median()),
                "future_drop72_median": none_if_nan(selected.fut72d.median()),
                "dd96_median": none_if_nan(selected.dd96.median()),
            }
        )
    return base


if __name__ == "__main__":
    main()
