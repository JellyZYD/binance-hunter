"""Compare 15m and 5m long-entry LGB experiments.

This is an experiment script only. It does not overwrite production models.

The 5m variants keep the same real-time windows as production 15m logic:
30m thrust, 2h body breakout, and 4h/12h/24h heat filters.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.metrics import average_precision_score, roc_auc_score

from ml_experiments.train_dump_5m_compare import (
    BASE_INTERVAL_MS,
    FEATS,
    Variant,
    aggregate,
    bars_15m_units,
    data_end,
    iso_ms,
    none_if_nan,
    parquet_files,
    compute_features_interval,
)
from pump_dump_hunter.ml.features import (
    LONG_CPOS_MIN,
    LONG_DIST_EMA21_MAX,
    LONG_HEAT_12H,
    LONG_HEAT_24H,
    LONG_HEAT_4H,
    LONG_RET2_MIN,
    LONG_UWICK_MAX,
    LONG_VOLR30_MIN,
    LOOKBACK,
    PUMP_12H,
    PUMP_1D,
    PUMP_4H,
    flow_columns,
)
from pump_dump_hunter.ml.train import DAY


VARIANT_15M = Variant("15m", 15 * 60_000, "native")
VARIANT_5M = Variant("5m_native", 5 * 60_000, "native")
MULTI_PREFIX = "m15_"
M15_FEATS = [f"{MULTI_PREFIX}{c}" for c in FEATS]
FLOW_FEATS = flow_columns()
SHORT_TERM_SPECS: dict[int, tuple[float, float]] = {
    2: (0.04, 0.06),
    4: (0.06, 0.07),
    8: (0.08, 0.08),
    12: (0.10, 0.09),
    24: (0.12, 0.10),
}
METRIC_HOURS = (2, 4, 8, 12, 24, 48)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("HUNTER_BB_SOURCE", r"E:\2C2G\币安数据库"))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="storage/ml/long_5m_compare.json")
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--variants", default="15m,5m_native,5m_multi")
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

    all_variants = {
        "15m": (VARIANT_15M, False),
        "5m_native": (VARIANT_5M, False),
        "5m_multi": (VARIANT_5M, True),
    }
    requested = [x.strip() for x in args.variants.split(",") if x.strip()]
    for name in requested:
        variant, multiscale = all_variants[name]
        print(f"building {name} from {len(files)} symbols", flush=True)
        df, feats = build_rows(source, files, start, end, variant, multiscale)
        print(
            f"{name}: rows={len(df)} events={df.event.nunique() if len(df) else 0} "
            f"symbols={df.symbol.nunique() if len(df) else 0} positive={df.y_long.mean() if len(df) else 0:.4f} "
            f"feats={len(feats)}",
            flush=True,
        )
        tasks: dict[str, Any] = {
            "pump48": fit_and_evaluate(df, feats, "y_long", 48),
        }
        for hours in SHORT_TERM_SPECS:
            tasks[f"gain_{hours}h"] = fit_and_evaluate(df, feats, f"y_gain_{hours}h", hours)
        result["variants"][name] = {
            "interval": variant.interval_label,
            "multiscale": multiscale,
            "feature_count": len(feats),
            "rows": int(len(df)),
            "events": int(df.event.nunique()) if len(df) else 0,
            "symbols": int(df.symbol.nunique()) if len(df) else 0,
            "tasks": tasks,
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def build_rows(
    source: Path,
    files: list[Path],
    start: int,
    end: int,
    variant: Variant,
    multiscale: bool,
) -> tuple[pd.DataFrame, list[str]]:
    load_start = start - 4 * DAY
    min_rows = bars_15m_units(LOOKBACK + 300, variant)
    feats = FEATS + FLOW_FEATS + (M15_FEATS if multiscale else [])

    symbol_data: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    rank_parts: list[pd.DataFrame] = []
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
        flow = compute_flow_features_interval(flow_frame(source, sym, g, load_start, end), variant)
        F = pd.concat([F.reset_index(drop=True), flow.reset_index(drop=True)], axis=1)
        if multiscale:
            if g15 is None or len(g15) < LOOKBACK + 300:
                continue
            F = attach_closed_15m_context(g, F, g15)
        symbol_data[sym] = (g, F)
        qv30 = g["qv"].rolling(bars_15m_units(2, variant)).sum()
        rank_parts.append(pd.DataFrame({"b": g["b"].values, "symbol": sym, "qv30": qv30.values}))
        if i % 25 == 0:
            print(f"{variant.name}{'+m15' if multiscale else ''}: prepared {i}/{len(files)}", flush=True)

    if not symbol_data:
        return pd.DataFrame(), feats

    rank_df = pd.concat(rank_parts, ignore_index=True)
    rank_df["qv30_rank"] = rank_df.groupby("b")["qv30"].rank(ascending=False, method="min")
    rank_lookup = {
        sym: grp.sort_values("b")["qv30_rank"].to_numpy(dtype=np.float64)
        for sym, grp in rank_df.groupby("symbol", sort=False)
    }

    rows: list[pd.DataFrame] = []
    horizon = bars_15m_units(max(METRIC_HOURS) * 4, variant)
    event_gap = bars_15m_units(96, variant)
    for sym, (g, F) in symbol_data.items():
        c, h, low = g["close"], g["high"], g["low"]
        n = len(g)
        ret16 = c / c.shift(bars_15m_units(16, variant)) - 1
        ret48 = c / c.shift(bars_15m_units(48, variant)) - 1
        ret96 = c / c.shift(bars_15m_units(96, variant)) - 1
        inpump = ((ret16 >= PUMP_4H) | (ret48 >= PUMP_12H) | (ret96 >= PUMP_1D)).fillna(False).to_numpy(bool)
        cand = long_setup_flags_interval(g, F, rank_lookup.get(sym), variant).to_numpy(bool)
        valid = np.zeros(n, bool)
        valid[bars_15m_units(LOOKBACK, variant):max(bars_15m_units(LOOKBACK, variant), n - horizon - 1)] = True
        finite = F[feats].notna().all(axis=1).to_numpy(bool)
        idx = np.where(cand & valid & finite)[0]
        if len(idx) == 0:
            continue

        fut_pump = np.zeros(n, bool)
        time_to_pump = np.full(n, np.nan)
        metrics: dict[str, np.ndarray] = {}
        for hours in METRIC_HOURS:
            metrics[f"future_gain_{hours}h"] = np.full(n, np.nan)
            metrics[f"future_drawdown_{hours}h"] = np.full(n, np.nan)
            metrics[f"future_close_ret_{hours}h"] = np.full(n, np.nan)
        for hours in SHORT_TERM_SPECS:
            metrics[f"trade_ret_{hours}h"] = np.full(n, np.nan)
            metrics[f"trade_win_{hours}h"] = np.full(n, np.nan)
        close_values = c.to_numpy(float)
        high_values = h.to_numpy(float)
        low_values = low.to_numpy(float)
        for ix in idx:
            end_ix = min(n, ix + 1 + horizon)
            future_flags = np.where(inpump[ix + 1:end_ix])[0]
            if len(future_flags):
                fut_pump[ix] = True
                time_to_pump[ix] = (int(future_flags[0]) + 1) * (variant.interval_ms / 60_000)
            for hours in METRIC_HOURS:
                bars = bars_15m_units(hours * 4, variant)
                metric_end = min(n, ix + 1 + bars)
                if ix + 1 >= metric_end:
                    continue
                metrics[f"future_gain_{hours}h"][ix] = np.nanmax(high_values[ix + 1:metric_end]) / close_values[ix] - 1
                metrics[f"future_drawdown_{hours}h"][ix] = close_values[ix] / np.nanmin(low_values[ix + 1:metric_end]) - 1
                metrics[f"future_close_ret_{hours}h"][ix] = close_values[metric_end - 1] / close_values[ix] - 1
            for hours, (target, stop) in SHORT_TERM_SPECS.items():
                bars = bars_15m_units(hours * 4, variant)
                metric_end = min(n, ix + 1 + bars)
                if ix + 1 >= metric_end:
                    continue
                trade_ret = conservative_long_trade_return(
                    close_values[ix],
                    high_values[ix + 1:metric_end],
                    low_values[ix + 1:metric_end],
                    close_values[metric_end - 1],
                    target,
                    stop,
                )
                metrics[f"trade_ret_{hours}h"][ix] = trade_ret
                metrics[f"trade_win_{hours}h"][ix] = float(trade_ret > 0)

        sub = F.iloc[idx][feats].copy()
        sub["y_long"] = fut_pump[idx].astype("int8")
        for name, values in metrics.items():
            sub[name] = values[idx]
        for hours, (target, stop) in SHORT_TERM_SPECS.items():
            sub[f"y_gain_{hours}h"] = (
                (sub[f"future_gain_{hours}h"] >= target)
                & (sub[f"future_drawdown_{hours}h"] <= stop)
            ).astype("int8")
            sub[f"target_gain_{hours}h"] = target
            sub[f"max_adverse_{hours}h"] = stop
        sub["time_to_pump_minutes"] = time_to_pump[idx]
        sub["entry_ret24h"] = ret96.to_numpy(float)[idx]
        sub["entry_ret4h"] = ret16.to_numpy(float)[idx]
        sub["ts"] = g["b"].values[idx]
        sub["symbol"] = sym
        ev = np.zeros(len(idx), int)
        k = 0
        for m in range(1, len(idx)):
            if idx[m] - idx[m - 1] > event_gap:
                k += 1
            ev[m] = k
        sub["event"] = [f"{sym}-{e}" for e in ev]
        rows.append(sub)

    if not rows:
        return pd.DataFrame(), feats
    return pd.concat(rows, ignore_index=True), feats


def conservative_long_trade_return(
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    close_at_horizon: float,
    target: float,
    stop: float,
) -> float:
    take = entry * (1 + target)
    cut = entry * (1 - stop)
    for high, low in zip(highs, lows):
        if low <= cut:
            return -stop
        if high >= take:
            return target
    return close_at_horizon / entry - 1


def long_setup_flags_interval(
    g: pd.DataFrame,
    f: pd.DataFrame,
    qv30_rank: np.ndarray | None,
    variant: Variant,
) -> pd.Series:
    c, o, qv = g["close"], g["open"], g["qv"]
    ret2 = c / c.shift(bars_15m_units(2, variant)) - 1
    ret16 = c / c.shift(bars_15m_units(16, variant)) - 1
    ret48 = c / c.shift(bars_15m_units(48, variant)) - 1
    ret96 = c / c.shift(bars_15m_units(96, variant)) - 1
    qv30 = qv.rolling(bars_15m_units(2, variant)).sum()
    volr30 = qv30 / qv30.rolling(bars_15m_units(20, variant)).mean()
    body_high = np.maximum(o, c)
    breakout = c > body_high.rolling(bars_15m_units(8, variant)).max().shift(1)
    inpump = (ret16 >= PUMP_4H) | (ret48 >= PUMP_12H) | (ret96 >= PUMP_1D)
    rank = pd.Series(qv30_rank if qv30_rank is not None else np.inf, index=g.index)
    return (
        (ret2 >= LONG_RET2_MIN)
        & (volr30 >= LONG_VOLR30_MIN)
        & (ret96 <= LONG_HEAT_24H)
        & (ret16 <= LONG_HEAT_4H)
        & (ret48 <= LONG_HEAT_12H)
        & breakout
        & (f["close_pos"] >= LONG_CPOS_MIN)
        & (f["uwick"] <= LONG_UWICK_MAX)
        & (f["dist_ema21"] > 0)
        & (f["dist_ema21"] <= LONG_DIST_EMA21_MAX)
        & (f["ema_spread"] > 0)
        & (~inpump)
        & (rank <= 150)
    )


def flow_frame(source: Path, sym: str, g: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    frame = pd.DataFrame({"b": g["b"].values, "close": g["close"].values})
    for group, cols, names in (
        ("oi", ["oi", "oi_value"], ["oi", "oival"]),
        ("global_acct_ratio", ["ratio"], ["lsg"]),
        ("top_pos_ratio", ["ratio"], ["lstp"]),
        ("taker_ratio", ["ratio"], ["tkr"]),
    ):
        data = read_market_state(source, group, sym, cols, start, end)
        if data is None:
            for name in names:
                frame[name] = np.nan
            continue
        merged = pd.merge_asof(frame[["b"]], data, left_on="b", right_on="timestamp", direction="backward")
        for col, name in zip(cols, names):
            frame[name] = merged[col].values
    return frame


def read_market_state(source: Path, group: str, sym: str, cols: list[str], start: int, end: int) -> pd.DataFrame | None:
    path = source / "market_state_hist" / group / f"{sym}.parquet"
    if not path.exists():
        return None
    try:
        return (
            pq.read_table(path, columns=["timestamp"] + cols, filters=[("timestamp", ">=", start), ("timestamp", "<=", end)])
            .to_pandas()
            .drop_duplicates("timestamp")
            .sort_values("timestamp")
        )
    except Exception:
        return None


def compute_flow_features_interval(flow_df: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    d = pd.DataFrame(index=flow_df.index)
    oi = flow_df["oi"]
    s16 = bars_15m_units(16, variant)
    s96 = bars_15m_units(96, variant)
    s8 = bars_15m_units(8, variant)
    d["oi_chg16"] = oi / oi.shift(s16) - 1
    d["oi_chg96"] = oi / oi.shift(s96) - 1
    d["oi_div16"] = d["oi_chg16"] - (flow_df["close"] / flow_df["close"].shift(s16) - 1)
    d["oival_chg16"] = flow_df["oival"] / flow_df["oival"].shift(s16) - 1
    lsg = flow_df["lsg"]
    d["ls_global"] = lsg
    d["ls_global_z"] = (lsg - lsg.rolling(s96).mean()) / lsg.rolling(s96).std().replace(0, np.nan)
    d["ls_top_pos"] = flow_df["lstp"]
    d["tk_ratio"] = flow_df["tkr"]
    d["tk_ma8"] = flow_df["tkr"].rolling(s8).mean()
    return d


def attach_closed_15m_context(g5: pd.DataFrame, f5: pd.DataFrame, g15: pd.DataFrame) -> pd.DataFrame:
    f15 = compute_features_interval(g15, VARIANT_15M).add_prefix(MULTI_PREFIX)
    right = pd.concat([g15[["b"]].reset_index(drop=True), f15.reset_index(drop=True)], axis=1)
    left = pd.DataFrame({"ctx_b": g5["b"].values - BASE_INTERVAL_MS})
    aligned = pd.merge_asof(left, right.sort_values("b"), left_on="ctx_b", right_on="b", direction="backward")
    aligned = aligned[M15_FEATS].reset_index(drop=True)
    return pd.concat([f5.reset_index(drop=True), aligned], axis=1)


def fit_and_evaluate(df: pd.DataFrame, feats: list[str], y_col: str, primary_hours: int) -> dict[str, Any]:
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
        "target": y_col,
        "primary_hours": primary_hours,
        "rows": int(len(df)),
        "events": int(df.event.nunique()),
        "symbols": int(df.symbol.nunique()),
        "train_rows": int(len(train)),
        "val_rows": int(len(val)),
        "positive_rate": float(df[y_col].mean()),
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
        out["thresholds"][f"train_q{int(q * 100)}"] = summarize(selected, val, threshold, y_col, primary_hours)
    return out


def summarize(selected: pd.DataFrame, val: pd.DataFrame, threshold: float, y_col: str, primary_hours: int) -> dict[str, Any]:
    if selected.empty:
        return {"threshold": threshold, "signals": 0}
    period_days = max(1e-9, (int(val.ts.max()) - int(val.ts.min())) / DAY)
    ttp = selected["time_to_pump_minutes"].replace([np.inf, -np.inf], np.nan)
    base = {
        "threshold": threshold,
        "signals": int(len(selected)),
        "per_day": float(len(selected) / period_days),
        f"event_precision_{y_col}": float(selected[y_col].mean()),
        "time_to_pump_median_minutes": none_if_nan(ttp.median()),
    }
    for hours in METRIC_HOURS:
        gain = selected[f"future_gain_{hours}h"]
        drawdown = selected[f"future_drawdown_{hours}h"]
        close_ret = selected[f"future_close_ret_{hours}h"]
        prefix = f"h{hours}"
        base[f"{prefix}_gain_median"] = none_if_nan(gain.median())
        base[f"{prefix}_gain_p75"] = none_if_nan(gain.quantile(0.75))
        base[f"{prefix}_drawdown_median"] = none_if_nan(drawdown.median())
        base[f"{prefix}_close_ret_median"] = none_if_nan(close_ret.median())
        base[f"{prefix}_gain_gt_drawdown"] = float((gain > drawdown).mean())
        base[f"{prefix}_gain_ge_5"] = float((gain >= 0.05).mean())
        base[f"{prefix}_gain_ge_10"] = float((gain >= 0.10).mean())
    if primary_hours in SHORT_TERM_SPECS:
        trade = selected[f"trade_ret_{primary_hours}h"]
        base["trade_target"] = SHORT_TERM_SPECS[primary_hours][0]
        base["trade_stop"] = SHORT_TERM_SPECS[primary_hours][1]
        base["trade_ret_mean"] = none_if_nan(trade.mean())
        base["trade_ret_median"] = none_if_nan(trade.median())
        base["trade_win_rate"] = float((trade > 0).mean())
        base["trade_loss_rate"] = float((trade < 0).mean())
    return base


if __name__ == "__main__":
    main()
