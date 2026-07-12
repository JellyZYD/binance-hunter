"""Train top-entry models with low-adverse labels over expanded history.

This is an experiment-only pipeline. It does not overwrite production models.

Goal:
  Find a "tradable top" signal, not merely a "will eventually drop" warning.
  Labels require a 72h future drop and cap the adverse move before the first
  -5% move. This matches the desired short entry: allow holding up to 72h, but
  avoid entries that first rip hard against the position.

Example:
    python ml_experiments/train_top_low_adverse.py --source "E:\\2C2G\\币安数据库" --days 0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.metrics import average_precision_score, roc_auc_score

from ml_experiments.train_dump_5m_compare import (
    FEATS,
    Variant,
    aggregate,
    bars_15m_units,
    data_end,
    fwd,
    iso_ms,
    none_if_nan,
    parquet_files,
    compute_features_interval,
)
from pump_dump_hunter.ml.features import LOOKBACK, PUMP_RUNUP
from pump_dump_hunter.ml.train import DAY

VARIANT_15M = Variant("15m", 15 * 60_000, "native")
VARIANT_5M = Variant("5m_native", 5 * 60_000, "native")
LABEL_SPECS = {
    "clean3_drop15": {"max_adv_before5": 0.03, "drop72": 0.15},
    "clean5_drop15": {"max_adv_before5": 0.05, "drop72": 0.15},
    "clean8_drop15": {"max_adv_before5": 0.08, "drop72": 0.15},
    "clean5_drop20": {"max_adv_before5": 0.05, "drop72": 0.20},
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=r"E:\2C2G\币安数据库")
    ap.add_argument("--days", type=int, default=0, help="0 means use all available history.")
    ap.add_argument("--variants", default="15m,5m")
    ap.add_argument("--out", default="storage/ml/top_low_adverse_full.json")
    ap.add_argument("--max-symbols", type=int, default=0)
    args = ap.parse_args(argv)

    source = Path(args.source)
    if not (source / "klines").is_dir():
        raise SystemExit(f"missing klines directory: {source / 'klines'}")

    files = parquet_files(source, args.max_symbols)
    end = data_end(files)
    start = data_start(files) if args.days <= 0 else end - args.days * DAY
    requested = [x.strip().lower() for x in args.variants.split(",") if x.strip()]
    variants = []
    if "15m" in requested:
        variants.append(VARIANT_15M)
    if "5m" in requested or "5m_native" in requested:
        variants.append(VARIANT_5M)

    result: dict[str, Any] = {
        "source": str(source),
        "days": args.days,
        "data_start": iso_ms(start),
        "data_end": iso_ms(end),
        "symbols_total": len(files),
        "label_specs": LABEL_SPECS,
        "variants": {},
    }

    for variant in variants:
        print(f"building {variant.name} from {len(files)} symbols", flush=True)
        df = build_rows(files, start, end, variant)
        print(
            f"{variant.name}: rows={len(df)} events={df.event.nunique()} symbols={df.symbol.nunique()} "
            + " ".join(f"{k}={df['y_' + k].mean():.4f}" for k in LABEL_SPECS),
            flush=True,
        )
        result["variants"][variant.name] = {
            "interval": variant.interval_label,
            "rows": int(len(df)),
            "events": int(df.event.nunique()),
            "symbols": int(df.symbol.nunique()),
            "tasks": {
                f"event_{name}": fit_and_evaluate(df, name, target_mode="event")
                for name in LABEL_SPECS
            }
            | {
                f"row_{name}": fit_and_evaluate(df, name, target_mode="row")
                for name in LABEL_SPECS
            },
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def data_start(files: list[Path]) -> int:
    start = 0
    for path in files:
        try:
            pf = pq.ParquetFile(path)
            for i in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(i).column(0).statistics
                if st and st.has_min_max:
                    value = int(st.min)
                    start = value if start == 0 else min(start, value)
        except Exception:
            continue
    return start


def build_rows(files: list[Path], start: int, end: int, variant: Variant) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    load_start = start - 4 * DAY
    future72 = bars_15m_units(288, variant)
    event_gap = bars_15m_units(48, variant)
    min_rows = bars_15m_units(LOOKBACK + 300, variant)
    lookback = bars_15m_units(LOOKBACK, variant)

    for i, path in enumerate(files, 1):
        sym = path.stem.upper()
        try:
            g = aggregate(path, load_start, end, variant)
        except Exception as exc:
            print(f"{variant.name}: skip {sym}: {exc}", flush=True)
            continue
        if g is None or len(g) < min_rows:
            continue

        F = compute_features_interval(g, variant)
        close = g.close.values
        high = g.high.values
        low = g.low.values
        n = len(g)
        fut72d = close / fwd(pd.Series(low), future72, "min").values - 1
        fut72u = fwd(pd.Series(high), future72, "max").values / close - 1
        future_min_close = close / fwd(pd.Series(g.close), future72, "min").values - 1
        future_max_close_up = fwd(pd.Series(g.close), future72, "max").values / close - 1

        watch = top_watch_flags(F).values
        valid = np.zeros(n, bool)
        valid[lookback:max(lookback, n - future72 - 1)] = True
        finite = np.isfinite(fut72d) & F[FEATS].notna().all(axis=1).values
        idx = np.where(watch & valid & finite)[0]
        if len(idx) == 0:
            continue

        hit5 = np.zeros(n, bool)
        adv_before5 = np.full(n, np.nan)
        time_to_5 = np.full(n, np.nan)
        max_up72_before_low = np.full(n, np.nan)
        for ix in idx:
            target = close[ix] * 0.95
            seg_hi = high[ix + 1 : ix + 1 + future72]
            seg_low = low[ix + 1 : ix + 1 + future72]
            hit = np.where(seg_low <= target)[0]
            if len(hit):
                j = int(hit[0])
                hit5[ix] = True
                adv_before5[ix] = seg_hi[: j + 1].max() / close[ix] - 1
                time_to_5[ix] = (j + 1) * (variant.interval_ms / 60_000)
            if len(seg_hi):
                max_up72_before_low[ix] = seg_hi.max() / close[ix] - 1

        sub = F.iloc[idx][FEATS].copy()
        sub["ts"] = g["b"].values[idx]
        sub["symbol"] = sym
        sub["hit5_72h"] = hit5[idx].astype("int8")
        sub["adv_before5"] = adv_before5[idx]
        sub["time_to_5_minutes"] = time_to_5[idx]
        sub["fut72d"] = fut72d[idx]
        sub["fut72u"] = fut72u[idx]
        sub["future_min_close_drop72"] = future_min_close[idx]
        sub["future_max_close_up72"] = future_max_close_up[idx]
        sub["max_up72"] = max_up72_before_low[idx]
        sub["dd96"] = F["dd_96"].values[idx]
        sub["runup96"] = F["runup_96"].values[idx]
        sub["watch_setup"] = 1

        ev = np.zeros(len(idx), int)
        k = 0
        for m in range(1, len(idx)):
            if idx[m] - idx[m - 1] > event_gap:
                k += 1
            ev[m] = k
        sub["event"] = [f"{sym}-{e}" for e in ev]
        for label, spec in LABEL_SPECS.items():
            good = (
                (sub["hit5_72h"] == 1)
                & (sub["adv_before5"] <= float(spec["max_adv_before5"]))
                & (sub["fut72d"] >= float(spec["drop72"]))
            )
            sub[f"good_{label}"] = good.astype("int8")
            sub[f"y_{label}"] = 0
            for _, grp in sub.groupby("event", sort=False):
                gd = grp[grp[f"good_{label}"] == 1]
                if len(gd):
                    sub.loc[gd["ts"].idxmin(), f"y_{label}"] = 1
        rows.append(sub)
        if i % 25 == 0:
            print(f"{variant.name}: loaded {i}/{len(files)} rows={sum(len(x) for x in rows)}", flush=True)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def top_watch_flags(f: pd.DataFrame | pd.Series) -> pd.Series:
    """Broad high-zone watch setup for monitored pump candidates.

    It is intentionally wider than the current production top setup so the model
    can score most monitored short candidates instead of only obvious upper-wick
    candles.
    """

    high_zone = (f["runup_96"] >= PUMP_RUNUP) & (f["dd_96"] >= -0.15)
    weak_or_rejection = (
        (f["close_pos"] <= 0.65)
        | (f["uwick"] >= 0.005)
        | (f["body"] < 0)
        | (f["ret_1"] <= 0.01)
    )
    not_vertical_extension = f["dist_ema21"] <= 0.35
    return high_zone & weak_or_rejection & not_vertical_extension


def fit_and_evaluate(df: pd.DataFrame, label: str, target_mode: str) -> dict[str, Any]:
    if target_mode not in {"event", "row"}:
        raise ValueError(f"unsupported target_mode: {target_mode}")
    y_col = f"y_{label}" if target_mode == "event" else f"good_{label}"
    good_col = f"good_{label}"
    if df.empty:
        return {"error": "empty dataset"}
    ts = df.ts.values
    cut = int(np.quantile(ts, 0.80))
    train = df[ts < cut].reset_index(drop=True)
    val = df[ts >= cut].reset_index(drop=True)
    pos = int(train[y_col].sum())
    neg = int(len(train) - pos)
    params = dict(
        objective="binary",
        n_estimators=360,
        learning_rate=0.025,
        num_leaves=32,
        min_child_samples=80,
        subsample=0.8,
        colsample_bytree=0.7,
        reg_lambda=1.5,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        verbosity=-1,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(train[FEATS], train[y_col])
    train_score = model.predict_proba(train[FEATS])[:, 1]
    val_score = model.predict_proba(val[FEATS])[:, 1]
    val = val.assign(score=val_score)
    auc = float("nan")
    ap = float("nan")
    if val[y_col].nunique() > 1:
        auc = float(roc_auc_score(val[y_col], val_score))
        ap = float(average_precision_score(val[y_col], val_score))
    out: dict[str, Any] = {
        "rows": int(len(df)),
        "events": int(df.event.nunique()),
        "train_rows": int(len(train)),
        "val_rows": int(len(val)),
        "positive_rate": float(df[y_col].mean()),
        "val_positive_rate": float(val[y_col].mean()),
        "val_good_row_rate": float(val[good_col].mean()),
        "target_mode": target_mode,
        "target_col": y_col,
        "val_auc": auc,
        "val_ap": ap,
        "train_start": iso_ms(int(train.ts.min())),
        "train_end": iso_ms(int(train.ts.max())),
        "val_start": iso_ms(int(val.ts.min())),
        "val_end": iso_ms(int(val.ts.max())),
        "thresholds": {},
    }
    for q in (0.30, 0.50, 0.70, 0.80, 0.90, 0.95, 0.98, 0.99):
        threshold = float(np.quantile(train_score, q))
        selected = val[val.score >= threshold].copy()
        selected = selected.sort_values(["symbol", "ts"]).groupby("event", sort=False, as_index=False).first()
        out["thresholds"][f"train_q{int(q * 100)}"] = summarize(selected, val, threshold, label, y_col)
    return out


def summarize(selected: pd.DataFrame, val: pd.DataFrame, threshold: float, label: str, target_col: str) -> dict[str, Any]:
    if selected.empty:
        return {"threshold": threshold, "signals": 0}
    period_days = max(1e-9, (int(val.ts.max()) - int(val.ts.min())) / DAY)
    adv = selected["adv_before5"].replace([np.inf, -np.inf], np.nan)
    t5 = selected["time_to_5_minutes"].replace([np.inf, -np.inf], np.nan)
    good_col = f"good_{label}"
    return {
        "threshold": threshold,
        "signals": int(len(selected)),
        "per_day": float(len(selected) / period_days),
        "event_coverage": float(len(selected) / max(1, val.event.nunique())),
        "target_precision": float(selected[target_col].mean()),
        f"event_precision_y_{label}": float(selected[f"y_{label}"].mean()),
        "row_good_rate": float(selected[good_col].mean()),
        "hit5_72h": float(selected.hit5_72h.mean()),
        "hit10_72h": float((selected.fut72d >= 0.10).mean()),
        "hit15_72h": float((selected.fut72d >= 0.15).mean()),
        "hit20_72h": float((selected.fut72d >= 0.20).mean()),
        "hit30_72h": float((selected.fut72d >= 0.30).mean()),
        "mae_before5_median": none_if_nan(adv.median()),
        "mae_before5_p75": none_if_nan(adv.quantile(0.75)),
        "mae_before5_p90": none_if_nan(adv.quantile(0.90)),
        "future_up72_median": none_if_nan(selected.fut72u.median()),
        "future_up72_p75": none_if_nan(selected.fut72u.quantile(0.75)),
        "future_drop72_median": none_if_nan(selected.fut72d.median()),
        "future_drop72_p75": none_if_nan(selected.fut72d.quantile(0.75)),
        "time_to_5_median_minutes": none_if_nan(t5.median()),
        "quick5_2h": float((selected.time_to_5_minutes <= 120).mean()),
        "quick5_6h": float((selected.time_to_5_minutes <= 360).mean()),
        "quick5_12h": float((selected.time_to_5_minutes <= 720).mean()),
    }


if __name__ == "__main__":
    main()
