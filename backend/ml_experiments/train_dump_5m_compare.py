"""Compare the current 15m dump LGB target with 5m variants.

This is an experiment script only. It does not overwrite production models.

Example:
    python ml_experiments/train_dump_5m_compare.py --source "E:\\2C2G\\币安数据库" --days 365
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pandas.errors import PerformanceWarning
from sklearn.metrics import average_precision_score, roc_auc_score

from pump_dump_hunter.ml.features import (
    DUMP_CPOS,
    DUMP_DD96,
    LOOKBACK,
    N_RAW,
    PUMP_RUNUP,
    feature_columns,
)
from pump_dump_hunter.ml.train import DAY, EXCLUDE

warnings.simplefilter("ignore", PerformanceWarning)

BASE_INTERVAL_MS = 15 * 60_000
FEATS = feature_columns()


@dataclass(frozen=True)
class Variant:
    name: str
    interval_ms: int
    raw_lag_mode: str = "native"  # native: last 5m bars; scaled: 15m-equivalent lag spacing

    @property
    def scale(self) -> int:
        if BASE_INTERVAL_MS % self.interval_ms != 0:
            raise ValueError(f"{self.name}: interval must divide 15m")
        return BASE_INTERVAL_MS // self.interval_ms

    @property
    def interval_label(self) -> str:
        return f"{self.interval_ms // 60_000}m"


VARIANTS = {
    "15m": Variant("15m", 15 * 60_000, "native"),
    "5m_native": Variant("5m_native", 5 * 60_000, "native"),
    "5m_scaled": Variant("5m_scaled", 5 * 60_000, "scaled"),
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("HUNTER_BB_SOURCE", r"E:\2C2G\币安数据库"))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--variants", default="15m,5m_native,5m_scaled")
    ap.add_argument("--out", default="storage/ml/dump_5m_compare_full.json")
    ap.add_argument("--max-symbols", type=int, default=0)
    args = ap.parse_args(argv)

    source = Path(args.source)
    if not (source / "klines").is_dir():
        raise SystemExit(f"missing klines directory: {source / 'klines'}")

    requested = [x.strip() for x in args.variants.split(",") if x.strip()]
    variants = [VARIANTS[x] for x in requested]
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

    for variant in variants:
        print(f"building {variant.name} from {len(files)} symbols", flush=True)
        df = build_rows(files, start, end, variant)
        print(
            f"{variant.name}: rows={len(df)} events={df.event.nunique()} symbols={df.symbol.nunique()} "
            f"positive={df.y_dump.mean():.4f}",
            flush=True,
        )
        result["variants"][variant.name] = fit_and_evaluate(df, variant)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def parquet_files(source: Path, max_symbols: int) -> list[Path]:
    files = [
        Path(f)
        for f in glob.glob(str(source / "klines" / "*.parquet"))
        if Path(f).stem.upper() not in EXCLUDE
    ]
    files.sort(key=lambda p: p.stem.upper())
    if max_symbols > 0:
        files = files[:max_symbols]
    return files


def data_end(files: list[Path]) -> int:
    end = 0
    for path in files:
        try:
            pf = pq.ParquetFile(path)
            for i in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(i).column(0).statistics
                if st and st.has_min_max:
                    end = max(end, int(st.max))
        except Exception:
            continue
    return end or int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")


def bars_15m_units(units: int, variant: Variant) -> int:
    return max(1, units * variant.scale)


def aggregate(path: Path, start: int, end: int, variant: Variant) -> pd.DataFrame | None:
    table = pq.read_table(
        path,
        columns=["timestamp", "open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume"],
        filters=[("timestamp", ">=", start), ("timestamp", "<=", end)],
    ).to_pandas()
    if table.empty:
        return None
    table = table.drop_duplicates("timestamp").sort_values("timestamp")
    table["b"] = (table.timestamp // variant.interval_ms) * variant.interval_ms
    grouped = table.groupby("b").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        qv=("quote_volume", "sum"),
        tbq=("taker_buy_quote_volume", "sum"),
        cnt=("close", "size"),
    )
    min_count = max(1, int(np.ceil((variant.interval_ms // 60_000) * 2 / 3)))
    return grouped[grouped.cnt >= min_count].reset_index()


def compute_features_interval(df: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    o, h, low, c = df["open"], df["high"], df["low"], df["close"]
    qv, tbq = df["qv"], df["tbq"]
    rng = (h - low).replace(0, np.nan)
    ret1_native = c / c.shift(1) - 1
    close_pos = (c - low) / rng
    body = (c - o) / o
    uwick = (h - np.maximum(o, c)) / c
    lwick = (np.minimum(o, c) - low) / c
    volr20 = qv / qv.rolling(bars_15m_units(20, variant)).mean()
    tsell = 1 - tbq / qv.replace(0, np.nan)
    ema8 = c.ewm(span=bars_15m_units(8, variant), adjust=False).mean()
    ema21 = c.ewm(span=bars_15m_units(21, variant), adjust=False).mean()

    d = pd.DataFrame(index=df.index)
    for k in (1, 2, 3, 6, 12, 24, 48, 96):
        d[f"ret_{k}"] = c / c.shift(bars_15m_units(k, variant)) - 1
    for k in (8, 24, 96):
        d[f"dd_{k}"] = c / h.rolling(bars_15m_units(k, variant)).max() - 1
    for k in (24, 96):
        d[f"runup_{k}"] = c / low.rolling(bars_15m_units(k, variant)).min() - 1
    d["volr_20"] = volr20
    d["volr_48"] = qv / qv.rolling(bars_15m_units(48, variant)).mean()
    d["tsell"] = tsell
    d["tsell_ma8"] = tsell.rolling(bars_15m_units(8, variant)).mean()
    d["close_pos"] = close_pos
    d["body"] = body
    d["uwick"] = uwick
    d["lwick"] = lwick
    d["retstd_20"] = ret1_native.rolling(bars_15m_units(20, variant)).std()
    d["atr_14"] = ((h - low) / c).rolling(bars_15m_units(14, variant)).mean()
    d["dist_ema8"] = c / ema8 - 1
    d["dist_ema21"] = c / ema21 - 1
    d["ema_spread"] = ema8 / ema21 - 1
    d["accel"] = (c / c.shift(bars_15m_units(3, variant)) - 1) - (
        c.shift(bars_15m_units(3, variant)) / c.shift(bars_15m_units(6, variant)) - 1
    )
    d["new_high_96"] = (c >= h.rolling(bars_15m_units(96, variant)).max() * 0.999).astype("int8")
    d["consec"] = np.sign(body).rolling(bars_15m_units(3, variant)).sum()
    for lag in range(1, N_RAW + 1):
        raw_shift = lag if variant.raw_lag_mode == "native" else bars_15m_units(lag, variant)
        d[f"r_ret_{lag}"] = ret1_native.shift(raw_shift)
        d[f"r_cpos_{lag}"] = close_pos.shift(raw_shift)
        d[f"r_body_{lag}"] = body.shift(raw_shift)
        d[f"r_uw_{lag}"] = uwick.shift(raw_shift)
        d[f"r_lw_{lag}"] = lwick.shift(raw_shift)
        d[f"r_volr_{lag}"] = volr20.shift(raw_shift)
        d[f"r_ts_{lag}"] = tsell.shift(raw_shift)
    return d


def fwd(series: pd.Series, horizon: int, kind: str) -> pd.Series:
    rev = series.iloc[::-1]
    rolled = rev.rolling(horizon, min_periods=1).min() if kind == "min" else rev.rolling(horizon, min_periods=1).max()
    return rolled.iloc[::-1].shift(-1)


def dump_setup_flags_interval(f: pd.DataFrame | pd.Series) -> pd.Series:
    return (f["runup_96"] >= PUMP_RUNUP) & (f["dd_96"] <= DUMP_DD96) & (f["body"] < 0) & (f["close_pos"] <= DUMP_CPOS)


def build_rows(files: list[Path], start: int, end: int, variant: Variant) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    load_start = start - 4 * DAY
    future_horizon = bars_15m_units(288, variant)  # 72h
    hit5_horizon = bars_15m_units(48, variant)  # 12h
    event_gap = bars_15m_units(48, variant)
    min_rows = bars_15m_units(LOOKBACK + 300, variant)
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
        fut72d = close / fwd(pd.Series(low), future_horizon, "min").values - 1
        fut72u = fwd(pd.Series(high), future_horizon, "max").values / close - 1
        dump_setup = dump_setup_flags_interval(F).values
        valid = np.zeros(n, bool)
        valid[bars_15m_units(LOOKBACK, variant):max(bars_15m_units(LOOKBACK, variant), n - future_horizon - 1)] = True
        finite = np.isfinite(fut72d) & F[FEATS].notna().all(axis=1).values
        idx = np.where(dump_setup & valid & finite)[0]
        if len(idx) == 0:
            continue

        reached5 = np.zeros(n, bool)
        adv_before = np.full(n, np.nan)
        time_to_5 = np.full(n, np.nan)
        for ix in idx:
            target = close[ix] * 0.95
            seg_hi = high[ix + 1 : ix + 1 + hit5_horizon]
            seg_low = low[ix + 1 : ix + 1 + hit5_horizon]
            hit = np.where(seg_low <= target)[0]
            if len(hit):
                j = int(hit[0])
                reached5[ix] = True
                adv_before[ix] = seg_hi[: j + 1].max() / close[ix] - 1
                time_to_5[ix] = (j + 1) * (variant.interval_ms / 60_000)

        dump_good = dump_setup & reached5 & (np.nan_to_num(adv_before, nan=1.0) <= 0.08) & (fut72d >= 0.10)
        sub = F.iloc[idx][FEATS].copy()
        sub["dump_setup"] = 1
        sub["dump_good"] = dump_good[idx].astype("int8")
        sub["reached5"] = reached5[idx].astype("int8")
        sub["adv_before5"] = adv_before[idx]
        sub["time_to_5_minutes"] = time_to_5[idx]
        sub["fut72d"] = fut72d[idx]
        sub["fut72u"] = fut72u[idx]
        sub["ts"] = g["b"].values[idx]
        sub["symbol"] = sym
        ev = np.zeros(len(idx), int)
        k = 0
        for m in range(1, len(idx)):
            if idx[m] - idx[m - 1] > event_gap:
                k += 1
            ev[m] = k
        sub["event"] = [f"{sym}-{e}" for e in ev]
        sub["y_dump"] = 0
        for _, grp in sub.groupby("event", sort=False):
            good = grp[grp["dump_good"] == 1]
            if len(good):
                sub.loc[good["ts"].idxmin(), "y_dump"] = 1
        rows.append(sub)
        if i % 25 == 0:
            print(f"{variant.name}: loaded {i}/{len(files)} rows={sum(len(x) for x in rows)}", flush=True)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def fit_and_evaluate(df: pd.DataFrame, variant: Variant) -> dict[str, Any]:
    if df.empty:
        return {"error": "empty dataset"}
    ts = df.ts.values
    cut = int(np.quantile(ts, 0.80))
    train = df[ts < cut].reset_index(drop=True)
    val = df[ts >= cut].reset_index(drop=True)
    pos = int(train.y_dump.sum())
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
    model.fit(train[FEATS], train.y_dump)
    train_score = model.predict_proba(train[FEATS])[:, 1]
    val_score = model.predict_proba(val[FEATS])[:, 1]
    val = val.assign(score=val_score)
    auc = float("nan")
    ap = float("nan")
    if val.y_dump.nunique() > 1:
        auc = float(roc_auc_score(val.y_dump, val_score))
        ap = float(average_precision_score(val.y_dump, val_score))
    out: dict[str, Any] = {
        "interval": variant.interval_label,
        "raw_lag_mode": variant.raw_lag_mode,
        "rows": int(len(df)),
        "events": int(df.event.nunique()),
        "symbols": int(df.symbol.nunique()),
        "train_rows": int(len(train)),
        "val_rows": int(len(val)),
        "positive_rate": float(df.y_dump.mean()),
        "val_positive_rate": float(val.y_dump.mean()),
        "val_auc": auc,
        "val_ap": ap,
        "train_start": iso_ms(int(train.ts.min())),
        "train_end": iso_ms(int(train.ts.max())),
        "val_start": iso_ms(int(val.ts.min())),
        "val_end": iso_ms(int(val.ts.max())),
        "thresholds": {},
    }
    for q in (0.50, 0.70, 0.80, 0.90, 0.95, 0.98):
        threshold = float(np.quantile(train_score, q))
        selected = val[val.score >= threshold].copy()
        selected = selected.sort_values(["symbol", "ts"]).groupby("event", sort=False, as_index=False).first()
        out["thresholds"][f"train_q{int(q * 100)}"] = summarize(selected, val, threshold, variant)
    return out


def summarize(selected: pd.DataFrame, val: pd.DataFrame, threshold: float, variant: Variant) -> dict[str, Any]:
    if selected.empty:
        return {"threshold": threshold, "signals": 0}
    period_days = max(1e-9, (int(val.ts.max()) - int(val.ts.min())) / DAY)
    adv = selected["adv_before5"].replace([np.inf, -np.inf], np.nan)
    t5 = selected["time_to_5_minutes"].replace([np.inf, -np.inf], np.nan)
    return {
        "threshold": threshold,
        "signals": int(len(selected)),
        "per_day": float(len(selected) / period_days),
        "event_precision_y_dump": float(selected.y_dump.mean()),
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
        "decision_interval_minutes": int(variant.interval_ms // 60_000),
    }


def none_if_nan(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return None if not np.isfinite(out) else out


if __name__ == "__main__":
    main()
