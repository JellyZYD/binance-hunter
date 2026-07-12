from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from pump_dump_hunter.backtest import aggregate_1m_to_interval
from pump_dump_hunter.config import load_settings
from pump_dump_hunter.data.store import Store


RAW_LAGS = 12


@dataclass
class IntervalSpec:
    interval: str
    bars_per_hour: int

    @property
    def bars_24h(self) -> int:
        return 24 * self.bars_per_hour

    @property
    def bars_48h(self) -> int:
        return 48 * self.bars_per_hour

    @property
    def bars_12h(self) -> int:
        return 12 * self.bars_per_hour

    @property
    def minutes(self) -> int:
        return 60 // self.bars_per_hour


SPECS = {
    "15m": IntervalSpec("15m", 4),
    "5m": IntervalSpec("5m", 12),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="storage/hunter_bb_300_v2.db")
    ap.add_argument("--settings", default="config/settings.json")
    ap.add_argument("--intervals", default="15m,5m")
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--out", default="storage/ml/interval_dump_compare.json")
    args = ap.parse_args()

    settings = load_settings(args.settings)
    exclude = set(settings["universe"].get("exclude_symbols", [])) | set(settings.get("bb_import", {}).get("exclude_symbols", []))
    store = Store(args.db)
    symbols = [s for s in store.candle_symbols("1m") if s not in exclude]
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    result: dict[str, Any] = {"db": args.db, "symbols": len(symbols), "intervals": {}}
    for raw in [s.strip() for s in args.intervals.split(",") if s.strip()]:
        spec = SPECS[raw]
        ds = build_dataset(store, symbols, spec)
        result["intervals"][raw] = run_experiment(ds, spec)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


def build_dataset(store: Store, symbols: list[str], spec: IntervalSpec) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    for i, symbol in enumerate(symbols, 1):
        one = store.load_candles(symbol, "1m")
        bars = aggregate_1m_to_interval(one, spec.interval)
        if len(bars) < spec.bars_48h + spec.bars_24h + 20:
            continue
        frame = candles_to_frame(bars)
        feat = compute_features(frame, spec)
        labels = compute_labels(frame, spec)
        out = pd.concat([frame[["ts", "close"]], feat, labels], axis=1)
        out["symbol"] = symbol
        out["bar_index"] = np.arange(len(out), dtype=np.int32)
        rows.append(out)
        if i % 50 == 0:
            print(f"{spec.interval}: loaded {i}/{len(symbols)}", flush=True)
    if not rows:
        return pd.DataFrame()
    ds = pd.concat(rows, ignore_index=True)
    feature_cols = feature_columns()
    finite = ds[feature_cols].notna().all(axis=1)
    eligible = ds["setup"].fillna(False) & finite & ds["y_clean10"].notna()
    return ds.loc[eligible].reset_index(drop=True)


def candles_to_frame(bars: list[Any]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [b.close_time for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "qv": [b.quote_volume for b in bars],
            "tbq": [b.taker_buy_quote for b in bars],
        }
    )


def compute_features(df: pd.DataFrame, spec: IntervalSpec) -> pd.DataFrame:
    o, h, l, c, qv, tbq = df["open"], df["high"], df["low"], df["close"], df["qv"], df["tbq"]
    rng = (h - l).replace(0, np.nan)
    ret1 = c / c.shift(1) - 1
    close_pos = (c - l) / rng
    body = (c - o) / o
    uwick = (h - np.maximum(o, c)) / c
    lwick = (np.minimum(o, c) - l) / c
    tsell = 1 - tbq / qv.replace(0, np.nan)
    ema8 = c.ewm(span=8, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    d = pd.DataFrame(index=df.index)
    ret_windows = [1, 2, 3, 6, 12, 24, 48, 96, spec.bars_24h]
    for k in ret_windows:
        d[f"ret_{k}"] = c / c.shift(k) - 1
    for k in (8, 24, 96, spec.bars_24h):
        d[f"dd_{k}"] = c / h.rolling(k).max() - 1
    for k in (24, 96, spec.bars_24h):
        d[f"runup_{k}"] = c / l.rolling(k).min() - 1
    d["volr_20"] = qv / qv.rolling(20).mean()
    d["volr_48"] = qv / qv.rolling(48).mean()
    d["tsell"] = tsell
    d["tsell_ma8"] = tsell.rolling(8).mean()
    d["close_pos"] = close_pos
    d["body"] = body
    d["uwick"] = uwick
    d["lwick"] = lwick
    d["retstd_20"] = ret1.rolling(20).std()
    d["atr_14"] = ((h - l) / c).rolling(14).mean()
    d["dist_ema8"] = c / ema8 - 1
    d["dist_ema21"] = c / ema21 - 1
    d["ema_spread"] = ema8 / ema21 - 1
    d["accel"] = (c / c.shift(3) - 1) - (c.shift(3) / c.shift(6) - 1)
    d["new_high_24h"] = (c >= h.rolling(spec.bars_24h).max() * 0.999).astype("int8")
    d["consec"] = np.sign(body).rolling(3).sum()
    for lag in range(1, RAW_LAGS + 1):
        d[f"r_ret_{lag}"] = ret1.shift(lag)
        d[f"r_cpos_{lag}"] = close_pos.shift(lag)
        d[f"r_body_{lag}"] = body.shift(lag)
        d[f"r_uw_{lag}"] = uwick.shift(lag)
        d[f"r_lw_{lag}"] = lwick.shift(lag)
        d[f"r_volr_{lag}"] = d["volr_20"].shift(lag)
        d[f"r_ts_{lag}"] = tsell.shift(lag)
    d["setup"] = (
        (d[f"runup_{spec.bars_24h}"] >= 0.20)
        & (d[f"dd_{spec.bars_24h}"] <= -0.04)
        & (d["body"] < 0)
        & (d["close_pos"] <= 0.40)
    )
    return d


def feature_columns() -> list[str]:
    base = [
        c
        for c in [
            "volr_20",
            "volr_48",
            "tsell",
            "tsell_ma8",
            "close_pos",
            "body",
            "uwick",
            "lwick",
            "retstd_20",
            "atr_14",
            "dist_ema8",
            "dist_ema21",
            "ema_spread",
            "accel",
            "new_high_24h",
            "consec",
        ]
    ]
    # Use prefix matching later for interval-specific window columns.
    raw: list[str] = []
    for lag in range(1, RAW_LAGS + 1):
        raw += [f"r_ret_{lag}", f"r_cpos_{lag}", f"r_body_{lag}", f"r_uw_{lag}", f"r_lw_{lag}", f"r_volr_{lag}", f"r_ts_{lag}"]
    return base + raw


def all_feature_columns(ds: pd.DataFrame) -> list[str]:
    dynamic = [c for c in ds.columns if c.startswith(("ret_", "dd_", "runup_"))]
    return sorted(dynamic) + feature_columns()


def compute_labels(df: pd.DataFrame, spec: IntervalSpec) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]
    fut_min_48 = future_min(l, spec.bars_48h)
    fut_max_48 = future_max(h, spec.bars_48h)
    fut_close_48 = c.shift(-spec.bars_48h)
    drop48 = c / fut_min_48 - 1
    up48 = fut_max_48 / c - 1
    ret48 = c / fut_close_48 - 1
    reached5 = future_first_hit(l, c / 1.05, spec.bars_48h)
    mae_before5 = adverse_before_hit(h, c, reached5, spec.bars_48h)
    out = pd.DataFrame(index=df.index)
    out["future_drop48"] = drop48
    out["future_up48"] = up48
    out["ret48"] = ret48
    out["mae_before5"] = mae_before5
    out["time_to_5_bars"] = reached5
    out["y_clean10"] = ((drop48 >= 0.10) & (mae_before5 <= 0.08)).astype("float")
    out.loc[drop48.isna() | mae_before5.isna(), "y_clean10"] = np.nan
    return out


def future_min(s: pd.Series, n: int) -> pd.Series:
    return s.iloc[::-1].rolling(n, min_periods=n).min().iloc[::-1].shift(-1)


def future_max(s: pd.Series, n: int) -> pd.Series:
    return s.iloc[::-1].rolling(n, min_periods=n).max().iloc[::-1].shift(-1)


def future_first_hit(low: pd.Series, target: pd.Series, n: int) -> pd.Series:
    vals = low.to_numpy(dtype=float)
    targets = target.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals) - n - 1):
        fut = vals[i + 1 : i + 1 + n]
        hit = np.flatnonzero(fut <= targets[i])
        if hit.size:
            out[i] = float(hit[0] + 1)
    return pd.Series(out, index=low.index)


def adverse_before_hit(high: pd.Series, close: pd.Series, hit_bars: pd.Series, n: int) -> pd.Series:
    hv = high.to_numpy(dtype=float)
    cv = close.to_numpy(dtype=float)
    hit = hit_bars.to_numpy(dtype=float)
    out = np.full(len(hv), np.nan)
    for i in range(len(hv) - n - 1):
        end = int(hit[i]) if math.isfinite(hit[i]) else n
        end = max(1, min(end, n))
        out[i] = float(np.max(hv[i + 1 : i + 1 + end]) / cv[i] - 1)
    return pd.Series(out, index=high.index)


def run_experiment(ds: pd.DataFrame, spec: IntervalSpec) -> dict[str, Any]:
    if ds.empty:
        return {"error": "empty dataset"}
    feats = all_feature_columns(ds)
    ds = ds.sort_values("ts").reset_index(drop=True)
    train_cut = int(ds["ts"].quantile(0.70))
    embargo = 24 * 3_600_000
    train = ds[ds["ts"] <= train_cut]
    val = ds[ds["ts"] > train_cut + embargo]
    model = fit_lgb(train, feats)
    train_score = model.predict_proba(train[feats])[:, 1]
    val_score = model.predict_proba(val[feats])[:, 1]
    auc = roc_auc_score(val["y_clean10"], val_score) if val["y_clean10"].nunique() > 1 else float("nan")
    ap = average_precision_score(val["y_clean10"], val_score) if val["y_clean10"].nunique() > 1 else float("nan")
    out: dict[str, Any] = {
        "rows": int(len(ds)),
        "train_rows": int(len(train)),
        "val_rows": int(len(val)),
        "positive_rate": float(ds["y_clean10"].mean()),
        "val_positive_rate": float(val["y_clean10"].mean()),
        "val_auc": float(auc),
        "val_ap": float(ap),
        "thresholds": {},
    }
    for q in (0.50, 0.70, 0.80, 0.90, 0.95):
        thr = float(np.quantile(train_score, q))
        sig = val.assign(score=val_score)
        sig = sig[sig["score"] >= thr]
        sig = dedupe_signals(sig, spec)
        out["thresholds"][f"train_q{int(q*100)}"] = summarize_signals(sig, spec)
    return out


def fit_lgb(train: pd.DataFrame, feats: list[str]) -> LGBMClassifier:
    y = train["y_clean10"].astype(int)
    pos = max(int(y.sum()), 1)
    neg = max(int(len(y) - pos), 1)
    model = LGBMClassifier(
        objective="binary",
        n_estimators=260,
        learning_rate=0.035,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.82,
        colsample_bytree=0.82,
        reg_lambda=1.5,
        random_state=42,
        n_jobs=4,
        verbose=-1,
        scale_pos_weight=max(1.0, neg / pos),
    )
    model.fit(train[feats], y)
    return model


def dedupe_signals(sig: pd.DataFrame, spec: IntervalSpec) -> pd.DataFrame:
    # 5m will cluster heavily. Keep at most one signal per symbol per 2 hours.
    gap = 2 * 3_600_000
    kept = []
    for _symbol, group in sig.sort_values(["symbol", "ts", "score"], ascending=[True, True, False]).groupby("symbol"):
        last = -10**18
        for row in group.itertuples(index=False):
            if int(row.ts) >= last + gap:
                kept.append(row._asdict())
                last = int(row.ts)
    return pd.DataFrame(kept)


def summarize_signals(sig: pd.DataFrame, spec: IntervalSpec) -> dict[str, Any]:
    n = int(len(sig))
    if n == 0:
        return {"signals": 0}
    return {
        "signals": n,
        "per_day": float(n / days_span(sig["ts"])),
        "precision_clean10": float(sig["y_clean10"].mean()),
        "big15_low48": float(((sig["future_drop48"] >= 0.15) & (sig["ret48"] > 0.05)).mean()),
        "hit5": float((sig["future_drop48"] >= 0.05).mean()),
        "hit10": float((sig["future_drop48"] >= 0.10).mean()),
        "hit15": float((sig["future_drop48"] >= 0.15).mean()),
        "hit20": float((sig["future_drop48"] >= 0.20).mean()),
        "mae_before5_median": float(sig["mae_before5"].median()),
        "mae_before5_p75": float(sig["mae_before5"].quantile(0.75)),
        "future_up48_median": float(sig["future_up48"].median()),
        "future_drop48_median": float(sig["future_drop48"].median()),
        "time_to_5_median_minutes": float(sig["time_to_5_bars"].dropna().median() * spec.minutes) if sig["time_to_5_bars"].notna().any() else None,
        "quick5_2h": float((sig["time_to_5_bars"] <= (120 // spec.minutes)).mean()),
    }


def days_span(ts: pd.Series) -> float:
    if ts.empty:
        return 1.0
    return max(1.0, (float(ts.max()) - float(ts.min())) / 86_400_000.0)


if __name__ == "__main__":
    raise SystemExit(main())
