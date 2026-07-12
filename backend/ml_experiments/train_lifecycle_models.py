"""Train lifecycle models for long-entry -> family -> exit/short.

This experiment treats an altcoin pump as one lifecycle:

1. long_pump_event: detect the early thrust that is likely to become a pump.
   long_start_quality separately checks whether that entry avoids early adverse
   chop while reaching a meaningful rally.
2. family: after the long signal, update the probable realized pump family.
3. flat_long: signal that a long should be closed.
4. short_start: signal that a short can start with limited adverse move.

All features are built from closed candles available at the decision bar. Future
paths are used only as training labels and evaluation targets.

This script writes experiment artifacts only and does not modify production
models under pump_dump_hunter/ml/models.

Example:
    python -m ml_experiments.train_lifecycle_models --source "E:\\2C2G\\币安数据库" --days 365
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    log_loss,
    roc_auc_score,
)

from ml_experiments.train_dump_5m_compare import (
    FEATS,
    Variant,
    aggregate,
    data_end,
    iso_ms,
    parquet_files,
    compute_features_interval,
)
from ml_experiments.train_event_family_classifier import (
    FAMILY_MAP,
    FAMILY_ORDER,
    OPERATIONAL_TARGETS,
)
from pump_dump_hunter.ml import features as mlf
from pump_dump_hunter.ml.train import DAY

VARIANT_15M = Variant("15m", 15 * 60_000, "native")
VARIANT_5M = Variant("5m", 5 * 60_000, "native")
VARIANT_5M_SCALED = Variant("5m_scaled", 5 * 60_000, "scaled")
ACTIVE_VARIANT = VARIANT_15M
BAR_MS = 15 * 60_000
HOUR_MS = 3_600_000

LONG_EXTRA = [
    "qv30_rank",
    "ret30_rank",
    "qv30_rank_pct",
    "ret30_rank_pct",
    "qv30",
    "qv30_ratio",
    "body_break_8",
]
ENTRY_CONTEXT = [
    "ctx_bars_since_entry",
    "ctx_hours_since_entry",
    "ctx_ret_since_entry",
    "ctx_high_since_entry",
    "ctx_low_since_entry",
    "ctx_drawdown_from_entry_high",
    "ctx_qv_sum_ratio",
    "ctx_qv_recent_ratio",
    "ctx_taker_sell_mean",
    "ctx_red_bar_share",
    "ctx_new_high_since_entry",
]
LONG_FEATURES = FEATS + LONG_EXTRA
STATE_FEATURES = FEATS + ENTRY_CONTEXT


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=r"E:\2C2G\币安数据库")
    ap.add_argument("--events", default="storage/ml/pump_events_clustered.parquet")
    ap.add_argument("--days", type=int, default=365, help="0 means all available history.")
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--out", default="storage/ml/lifecycle_models.json")
    ap.add_argument("--report", default="storage/ml/lifecycle_models.md")
    ap.add_argument("--dataset-dir", default="storage/ml/lifecycle")
    ap.add_argument("--model-dir", default="storage/ml/lifecycle_models")
    ap.add_argument("--long-horizon-hours", type=float, default=48.0)
    ap.add_argument("--state-horizon-hours", type=float, default=72.0)
    ap.add_argument("--entry-cooldown-hours", type=float, default=24.0)
    ap.add_argument("--interval", choices=("15m", "5m", "5m_scaled"), default="15m")
    args = ap.parse_args(argv)
    configure_interval(args.interval)

    source = Path(args.source)
    if not (source / "klines").is_dir():
        raise SystemExit(f"missing klines directory: {source / 'klines'}")
    events = load_events(Path(args.events))
    files = parquet_files(source, args.max_symbols)
    end = data_end(files)
    start = min_event_start(events, files) if args.days <= 0 else end - int(args.days * DAY)
    files_by_symbol = {p.stem.upper(): p for p in files}

    print("building cross-sectional ranks", flush=True)
    ranks = build_rank_frame(files, start, end)
    print(f"rank rows={len(ranks)}", flush=True)

    entries, states = build_lifecycle_rows(
        files_by_symbol,
        ranks,
        events,
        start,
        end,
        long_horizon_bars=hours_to_bars(args.long_horizon_hours),
        state_horizon_bars=hours_to_bars(args.state_horizon_hours),
        cooldown_bars=hours_to_bars(args.entry_cooldown_hours),
    )
    if entries.empty:
        raise SystemExit("no lifecycle entry rows built")
    if states.empty:
        raise SystemExit("no lifecycle state rows built")

    dataset_dir = Path(args.dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    entries_path = dataset_dir / "long_entries.parquet"
    states_path = dataset_dir / "state_rows.parquet"
    entries.to_parquet(entries_path, index=False)
    states.to_parquet(states_path, index=False)

    model_dir = Path(args.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "source": str(source),
        "events": str(args.events),
        "days": args.days,
        "data_start": iso_ms(int(start)),
        "data_end": iso_ms(int(end)),
        "entries_dataset": str(entries_path),
        "states_dataset": str(states_path),
        "long_horizon_hours": args.long_horizon_hours,
        "state_horizon_hours": args.state_horizon_hours,
        "entry_cooldown_hours": args.entry_cooldown_hours,
        "interval": args.interval,
        "bar_minutes": int(BAR_MS / 60_000),
        "symbols": int(entries.symbol.nunique()),
        "long_entries": int(len(entries)),
        "state_rows": int(len(states)),
        "linked_events": int(entries.linked_event_id.notna().sum()),
        "family_counts": entries.dropna(subset=["family"]).family.value_counts().to_dict(),
        "models": {},
    }

    print("training long_pump_event", flush=True)
    result["models"]["long_pump_event"], long_pump_model = fit_binary(
        entries,
        "y_pump_event",
        LONG_FEATURES,
        eval_cols=["future_high_48h", "adverse_before_up5", "linked_event_id", "family"],
    )
    long_pump_model.booster_.save_model(str(model_dir / "long_pump_event.txt"))

    print("training long_start_quality", flush=True)
    result["models"]["long_start_quality"], long_model = fit_binary(
        entries,
        "y_long_start",
        LONG_FEATURES,
        eval_cols=["future_high_48h", "adverse_before_up5", "linked_event_id", "family"],
    )
    long_model.booster_.save_model(str(model_dir / "long_start_quality.txt"))

    print("training family classifier", flush=True)
    fam_rows = states[states["family"].notna()].copy()
    result["models"]["family"], family_model = fit_family(fam_rows, STATE_FEATURES)
    family_model.booster_.save_model(str(model_dir / "family.txt"))

    print("training flat_long", flush=True)
    result["models"]["flat_long"], flat_model = fit_binary(
        states,
        "y_flat_long",
        STATE_FEATURES,
        eval_cols=["family", "stage_hours", "future_up24", "future_drop72"],
    )
    flat_model.booster_.save_model(str(model_dir / "flat_long.txt"))

    print("training short_start", flush=True)
    result["models"]["short_start"], short_model = fit_binary(
        states,
        "y_short_start",
        STATE_FEATURES,
        eval_cols=["family", "stage_hours", "short_adverse_before5", "future_drop72"],
    )
    short_model.booster_.save_model(str(model_dir / "short_start.txt"))

    result["models"]["family_experts"] = fit_family_experts(states)
    result["model_dir"] = str(model_dir)
    result["feature_sets"] = {
        "long_features": LONG_FEATURES,
        "state_features": STATE_FEATURES,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.report).write_text(render_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def load_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"missing events file: {path}")
    events = pd.read_parquet(path).copy()
    events["family"] = events["cluster"].map(FAMILY_MAP)
    events = events[events.family.notna()].copy()
    events["event_id"] = (
        events["symbol"].astype(str)
        + "-"
        + events["start_time"].astype("int64").astype(str)
        + "-"
        + events["trigger_time"].astype("int64").astype(str)
    )
    return events.sort_values(["symbol", "trigger_time"]).reset_index(drop=True)


def min_event_start(events: pd.DataFrame, files: list[Path]) -> int:
    if not events.empty:
        return int(events.start_time.min()) - 7 * DAY
    return data_end(files) - 365 * DAY


def build_rank_frame(files: list[Path], start: int, end: int) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for i, path in enumerate(files, 1):
        try:
            g = aggregate(path, start - 2 * DAY, end, ACTIVE_VARIANT)
        except Exception as exc:
            print(f"rank skip {path.stem.upper()}: {exc}", flush=True)
            continue
        if g is None or len(g) < 200:
            continue
        bars_30m = hours_to_bars(0.5)
        qv30 = g["qv"].rolling(bars_30m).sum()
        ret30 = g["close"] / g["close"].shift(bars_30m) - 1.0
        frames.append(pd.DataFrame({"symbol": path.stem.upper(), "b": g["b"].values, "qv30": qv30.values, "ret30": ret30.values}))
        if i % 40 == 0:
            print(f"rank loaded {i}/{len(files)}", flush=True)
    ranks = pd.concat(frames, ignore_index=True)
    ranks["qv30_rank"] = ranks.groupby("b")["qv30"].rank(method="min", ascending=False)
    ranks["ret30_rank"] = ranks.groupby("b")["ret30"].rank(method="min", ascending=False)
    counts = ranks.groupby("b")["symbol"].transform("count")
    ranks["qv30_rank_pct"] = ranks["qv30_rank"] / counts
    ranks["ret30_rank_pct"] = ranks["ret30_rank"] / counts
    return ranks[["symbol", "b", "qv30_rank", "ret30_rank", "qv30_rank_pct", "ret30_rank_pct"]]


def build_lifecycle_rows(
    files_by_symbol: dict[str, Path],
    ranks: pd.DataFrame,
    events: pd.DataFrame,
    start: int,
    end: int,
    long_horizon_bars: int,
    state_horizon_bars: int,
    cooldown_bars: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    entry_rows: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    stages = [0, 2, 4, 6, 8, 12, 18, 24, 36, 48, 60, 72]
    stages_bars = [hours_to_bars(x) for x in stages]
    ranks_by_symbol = {sym: g.drop(columns=["symbol"]) for sym, g in ranks.groupby("symbol", sort=False)}
    event_by_symbol = {sym: g.reset_index(drop=True) for sym, g in events.groupby("symbol", sort=False)}

    for i, (symbol, path) in enumerate(files_by_symbol.items(), 1):
        try:
            g = aggregate(path, start - 5 * DAY, end, ACTIVE_VARIANT)
        except Exception as exc:
            print(f"skip {symbol}: {exc}", flush=True)
            continue
        if g is None or len(g) < 500:
            continue
        rank = ranks_by_symbol.get(symbol)
        if rank is None:
            continue
        g = g.merge(rank, on="b", how="left")
        F = compute_features_interval(g, ACTIVE_VARIANT)
        add_long_extras(g, F)
        cand = long_candidate_flags(g, F)
        valid = np.zeros(len(g), dtype=bool)
        valid[mlf.LOOKBACK : max(mlf.LOOKBACK, len(g) - state_horizon_bars - 2)] = True
        finite = F[FEATS].notna().all(axis=1).values
        candidate_ixs = dedup_indices(np.where(cand & valid & finite)[0], cooldown_bars)
        sym_events = event_by_symbol.get(symbol, pd.DataFrame())
        for ix in candidate_ixs:
            entry = build_entry_row(symbol, g, F, ix, long_horizon_bars, sym_events)
            if entry is None:
                continue
            entry_rows.append(entry)
            for stage_h, offset in zip(stages, stages_bars):
                state_ix = ix + offset
                if state_ix >= len(g) - state_horizon_bars - 2:
                    continue
                if state_ix < ix or F.iloc[state_ix][FEATS].isna().any():
                    continue
                state = build_state_row(symbol, g, F, ix, state_ix, state_horizon_bars, entry, stage_h)
                if state is not None:
                    state_rows.append(state)
        if i % 25 == 0:
            print(f"lifecycle loaded {i}/{len(files_by_symbol)} entries={len(entry_rows)} states={len(state_rows)}", flush=True)
    return pd.DataFrame(entry_rows), pd.DataFrame(state_rows)


def add_long_extras(g: pd.DataFrame, f: pd.DataFrame) -> None:
    qv30 = g["qv"].rolling(hours_to_bars(0.5)).sum()
    g["qv30"] = qv30
    g["qv30_ratio"] = qv30 / qv30.shift(1).rolling(hours_to_bars(5.0)).mean()
    body_high = pd.concat([g["open"], g["close"]], axis=1).max(axis=1)
    g["body_break_8"] = (g["close"] > body_high.shift(1).rolling(hours_to_bars(2.0)).max()).astype("int8")
    for col in LONG_EXTRA:
        if col in g.columns:
            f[col] = g[col]


def long_candidate_flags(g: pd.DataFrame, f: pd.DataFrame) -> np.ndarray:
    c, qv = g["close"], g["qv"]
    ret30 = c / c.shift(hours_to_bars(0.5)) - 1
    ret4h = c / c.shift(hours_to_bars(4)) - 1
    ret12h = c / c.shift(hours_to_bars(12)) - 1
    ret24h = c / c.shift(hours_to_bars(24)) - 1
    qv30 = qv.rolling(hours_to_bars(0.5)).sum()
    volr30 = qv30 / qv30.rolling(hours_to_bars(5.0)).mean()
    inpump = (ret4h >= mlf.PUMP_4H) | (ret12h >= mlf.PUMP_12H) | (ret24h >= mlf.PUMP_1D)
    base = (
        (ret30 >= mlf.LONG_RET2_MIN)
        & (volr30 >= mlf.LONG_VOLR30_MIN)
        & (g["body_break_8"] > 0)
        & (ret24h <= mlf.LONG_HEAT_24H)
        & (ret4h <= mlf.LONG_HEAT_4H)
        & (ret12h <= mlf.LONG_HEAT_12H)
        & (f["close_pos"] >= mlf.LONG_CPOS_MIN)
        & (f["uwick"] <= mlf.LONG_UWICK_MAX)
        & (f["dist_ema21"] > 0)
        & (f["dist_ema21"] <= mlf.LONG_DIST_EMA21_MAX)
        & (f["ema_spread"] > 0)
        & (~inpump)
    )
    return (
        base
        & (g["qv30_rank"] <= 150)
        & np.isfinite(g["qv30_rank"])
        & f[LONG_EXTRA].notna().all(axis=1)
    ).values


def dedup_indices(indices: np.ndarray, cooldown_bars: int) -> list[int]:
    out: list[int] = []
    last = -10**9
    for ix in indices:
        if int(ix) - last >= cooldown_bars:
            out.append(int(ix))
            last = int(ix)
    return out


def build_entry_row(
    symbol: str,
    g: pd.DataFrame,
    f: pd.DataFrame,
    ix: int,
    horizon_bars: int,
    events: pd.DataFrame,
) -> dict[str, Any] | None:
    close = g["close"].values
    high = g["high"].values
    low = g["low"].values
    end = min(len(g), ix + horizon_bars + 1)
    if end <= ix + 2:
        return None
    entry_price = float(close[ix])
    future_high = float(np.max(high[ix + 1 : end]) / entry_price - 1.0)
    future_low = float(entry_price / np.min(low[ix + 1 : end]) - 1.0)
    hit_up5 = np.where(high[ix + 1 : end] >= entry_price * 1.05)[0]
    if len(hit_up5):
        first_up = int(hit_up5[0]) + 1
        adverse_before_up5 = float(entry_price / np.min(low[ix + 1 : ix + first_up + 1]) - 1.0)
        minutes_to_up5 = int(first_up * BAR_MS / 60_000)
    else:
        adverse_before_up5 = future_low
        minutes_to_up5 = None
    linked = link_event(int(g.b.iloc[ix]), horizon_bars * BAR_MS, events)
    row = f.iloc[ix][LONG_FEATURES].to_dict()
    row.update(
        {
            "symbol": symbol,
            "entry_time": int(g.b.iloc[ix]),
            "entry_time_iso": iso_ms(int(g.b.iloc[ix])),
            "entry_price": entry_price,
            "future_high_48h": future_high,
            "future_adverse_48h": future_low,
            "adverse_before_up5": adverse_before_up5,
            "minutes_to_up5": minutes_to_up5,
            "linked_event_id": linked.get("event_id") if linked else None,
            "family": linked.get("family") if linked else None,
            "cluster": linked.get("cluster") if linked else None,
            "event_trigger_time": linked.get("trigger_time") if linked else None,
            "event_peak_time": linked.get("peak_time") if linked else None,
            "event_post_end_time": linked.get("post_end_time") if linked else None,
        }
    )
    row["y_pump_event"] = int(linked is not None)
    row["y_long_start"] = int(future_high >= 0.12 and adverse_before_up5 <= 0.08)
    return row


def link_event(entry_time: int, horizon_ms: int, events: pd.DataFrame) -> dict[str, Any] | None:
    if events.empty:
        return None
    linked = events[
        ((events["start_time"] <= entry_time) & (events["peak_time"] >= entry_time))
        | ((events["trigger_time"] > entry_time) & (events["trigger_time"] <= entry_time + horizon_ms))
    ]
    if linked.empty:
        return None
    row = linked.sort_values("trigger_time").iloc[0]
    return row.to_dict()


def build_state_row(
    symbol: str,
    g: pd.DataFrame,
    f: pd.DataFrame,
    entry_ix: int,
    ix: int,
    horizon_bars: int,
    entry: dict[str, Any],
    stage_h: int,
) -> dict[str, Any] | None:
    close = g["close"].values
    high = g["high"].values
    low = g["low"].values
    end72 = min(len(g), ix + horizon_bars + 1)
    end24 = min(len(g), ix + hours_to_bars(24) + 1)
    end12 = min(len(g), ix + hours_to_bars(12) + 1)
    if end72 <= ix + 2:
        return None
    price = float(close[ix])
    fut_up24 = float(np.max(high[ix + 1 : end24]) / price - 1.0) if end24 > ix + 1 else 0.0
    fut_drop72 = float(price / np.min(low[ix + 1 : end72]) - 1.0)
    fut_drop12 = float(price / np.min(low[ix + 1 : end12]) - 1.0) if end12 > ix + 1 else 0.0
    short_adv, short_minutes_to_down5 = short_adverse_before_down5(price, high, low, ix, end72)
    ctx = context_since_entry(g, entry_ix, ix)
    if any(not np.isfinite(v) for v in ctx.values()):
        return None
    row = f.iloc[ix][FEATS].to_dict()
    row.update(ctx)
    row.update(
        {
            "symbol": symbol,
            "entry_time": int(entry["entry_time"]),
            "decision_time": int(g.b.iloc[ix]),
            "decision_time_iso": iso_ms(int(g.b.iloc[ix])),
            "stage_hours": float(stage_h),
            "entry_price": float(entry["entry_price"]),
            "current_price": price,
            "family": entry.get("family"),
            "cluster": entry.get("cluster"),
            "linked_event_id": entry.get("linked_event_id"),
            "future_up24": fut_up24,
            "future_drop72": fut_drop72,
            "future_drop12": fut_drop12,
            "short_adverse_before5": short_adv,
            "short_minutes_to_down5": short_minutes_to_down5,
        }
    )
    row["y_flat_long"] = int(row["ctx_ret_since_entry"] >= 0.04 and fut_drop72 >= 0.10 and fut_up24 <= 0.06)
    row["y_short_start"] = int(fut_drop72 >= 0.15 and short_adv <= 0.05)
    row["y_continue_long"] = int(fut_up24 >= 0.08 and fut_drop12 <= 0.06)
    return row


def context_since_entry(g: pd.DataFrame, entry_ix: int, ix: int) -> dict[str, float]:
    close = g["close"].values
    high = g["high"].values
    low = g["low"].values
    open_ = g["open"].values
    qv = g["qv"].values
    tbq = g["tbq"].values
    bars = max(0, ix - entry_ix)
    seg = slice(entry_ix, ix + 1)
    pre = slice(max(0, entry_ix - hours_to_bars(24)), entry_ix)
    entry_close = float(close[entry_ix])
    seg_high = float(np.max(high[seg]))
    seg_low = float(np.min(low[seg]))
    pre_qv_mean = float(np.nanmean(qv[pre])) if entry_ix > 0 else float("nan")
    if not np.isfinite(pre_qv_mean) or pre_qv_mean <= 0:
        pre_qv_mean = float(np.nanmean(qv[max(0, ix - hours_to_bars(24)) : ix + 1]))
    recent_start = max(entry_ix, ix - hours_to_bars(3.75))
    recent_qv_mean = float(np.nanmean(qv[recent_start : ix + 1]))
    tsell = 1.0 - tbq[seg] / np.where(qv[seg] == 0, np.nan, qv[seg])
    bodies = close[seg] - open_[seg]
    return {
        "ctx_bars_since_entry": float(bars),
        "ctx_hours_since_entry": float(bars * BAR_MS / HOUR_MS),
        "ctx_ret_since_entry": float(close[ix] / entry_close - 1.0),
        "ctx_high_since_entry": float(seg_high / entry_close - 1.0),
        "ctx_low_since_entry": float(seg_low / entry_close - 1.0),
        "ctx_drawdown_from_entry_high": float(close[ix] / seg_high - 1.0),
        "ctx_qv_sum_ratio": float(np.nansum(qv[seg]) / (pre_qv_mean * max(1, bars + 1))),
        "ctx_qv_recent_ratio": float(recent_qv_mean / pre_qv_mean),
        "ctx_taker_sell_mean": float(np.nanmean(tsell)),
        "ctx_red_bar_share": float((bodies < 0).mean()),
        "ctx_new_high_since_entry": float(seg_high > high[entry_ix] * 1.001),
    }


def short_adverse_before_down5(price: float, high: np.ndarray, low: np.ndarray, ix: int, end: int) -> tuple[float, int | None]:
    target = price * 0.95
    hit = np.where(low[ix + 1 : end] <= target)[0]
    if len(hit):
        first = int(hit[0]) + 1
        return float(np.max(high[ix + 1 : ix + first + 1]) / price - 1.0), int(first * BAR_MS / 60_000)
    return float(np.max(high[ix + 1 : end]) / price - 1.0), None


def fit_binary(df: pd.DataFrame, target: str, features: list[str], eval_cols: list[str] | None = None) -> tuple[dict[str, Any], lgb.LGBMClassifier]:
    data = df[df[target].notna()].copy()
    split = time_split(data)
    train = data[split["train"]]
    hold = data[split["holdout"]]
    pos = int(train[target].sum())
    neg = int(len(train) - pos)
    params = dict(
        objective="binary",
        n_estimators=450,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=50,
        subsample=0.85,
        colsample_bytree=0.80,
        reg_lambda=2.0,
        scale_pos_weight=max(1.0, neg / max(pos, 1)),
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(train[features], train[target])
    score = model.predict_proba(hold[features])[:, 1] if len(hold) else np.array([])
    y = hold[target].astype(int).values
    report: dict[str, Any] = {
        "target": target,
        "rows": int(len(data)),
        "positives": int(data[target].sum()),
        "base_rate": safe_float(data[target].mean()),
        "split": split_payload(data, split),
        "holdout": binary_metrics(hold, y, score, eval_cols or []),
        "feature_importance": importance(model, features),
    }
    return report, model


def fit_family(df: pd.DataFrame, features: list[str]) -> tuple[dict[str, Any], lgb.LGBMClassifier]:
    data = df[df["family"].isin(FAMILY_ORDER)].copy()
    label_to_id = {v: i for i, v in enumerate(FAMILY_ORDER)}
    split = time_split(data)
    train = data[split["train"]]
    hold = data[split["holdout"]]
    y_train = train["family"].map(label_to_id).astype(int)
    y_hold = hold["family"].map(label_to_id).astype(int)
    params = dict(
        objective="multiclass",
        num_class=len(FAMILY_ORDER),
        n_estimators=450,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=35,
        subsample=0.85,
        colsample_bytree=0.80,
        reg_lambda=2.0,
        class_weight="balanced",
        n_jobs=-1,
        random_state=42,
        verbosity=-1,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(train[features], y_train)
    probs = model.predict_proba(hold[features])
    pred = np.argmax(probs, axis=1)
    report: dict[str, Any] = {
        "rows": int(len(data)),
        "events": int(data.linked_event_id.nunique()),
        "family_counts": data.family.value_counts().to_dict(),
        "split": split_payload(data, split),
        "holdout": {
            "accuracy": safe_float(accuracy_score(y_hold, pred)),
            "balanced_accuracy": safe_float(balanced_accuracy_score(y_hold, pred)),
            "log_loss": safe_float(log_loss(y_hold, probs, labels=list(range(len(FAMILY_ORDER))))),
            "by_stage": {},
            "operational": {},
        },
        "feature_importance": importance(model, features),
    }
    for stage, grp in hold.groupby("stage_hours", sort=True):
        idx = hold.index.get_indexer(grp.index)
        yp = y_hold.iloc[idx]
        pp = probs[idx]
        pr = np.argmax(pp, axis=1)
        report["holdout"]["by_stage"][str(stage)] = {
            "rows": int(len(grp)),
            "events": int(grp.linked_event_id.nunique()),
            "accuracy": safe_float(accuracy_score(yp, pr)),
            "balanced_accuracy": safe_float(balanced_accuracy_score(yp, pr)),
        }
    for name, families in OPERATIONAL_TARGETS.items():
        target_ids = [label_to_id[x] for x in families]
        score = probs[:, target_ids].sum(axis=1)
        y = hold.family.isin(families).astype(int).values
        report["holdout"]["operational"][name] = binary_metrics(hold, y, score, ["stage_hours", "family"])
    return report, model


def fit_family_experts(states: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for family in FAMILY_ORDER:
        grp = states[states["family"] == family].copy()
        out[family] = {}
        for target in ("y_flat_long", "y_short_start"):
            if len(grp) < 300 or grp[target].sum() < 25 or grp[target].nunique() < 2:
                out[family][target] = {
                    "skipped": True,
                    "rows": int(len(grp)),
                    "positives": int(grp[target].sum()) if len(grp) else 0,
                }
                continue
            report, _model = fit_binary(grp, target, STATE_FEATURES, eval_cols=["stage_hours", "future_drop72"])
            out[family][target] = report
    return out


def time_split(df: pd.DataFrame) -> dict[str, Any]:
    time_col = "entry_time" if "entry_time" in df.columns else "decision_time"
    unique_times = np.sort(df[time_col].dropna().unique())
    cut = float(np.quantile(unique_times, 0.80))
    embargo = 3 * DAY
    return {
        "time_col": time_col,
        "cut": cut,
        "embargo": embargo,
        "train": df[time_col] < cut,
        "holdout": df[time_col] >= cut + embargo,
    }


def split_payload(df: pd.DataFrame, split: dict[str, Any]) -> dict[str, Any]:
    train = df[split["train"]]
    hold = df[split["holdout"]]
    return {
        "time_col": split["time_col"],
        "cut": iso_ms(int(split["cut"])),
        "embargo_days": int(split["embargo"] / DAY),
        "train_rows": int(len(train)),
        "holdout_rows": int(len(hold)),
    }


def binary_metrics(df: pd.DataFrame, y: np.ndarray, score: np.ndarray, eval_cols: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "rows": int(len(df)),
        "positives": int(y.sum()) if len(y) else 0,
        "base_rate": safe_float(y.mean()) if len(y) else None,
        "auc": maybe_auc(y, score),
        "ap": maybe_ap(y, score),
        "thresholds": {},
        "by_family": {},
        "by_stage": {},
    }
    for q in (0.80, 0.90, 0.95, 0.98):
        out["thresholds"][f"q{int(q * 100)}"] = threshold_metrics(y, score, q)
    if "family" in eval_cols and "family" in df.columns:
        for family, grp in df.groupby("family", dropna=False):
            if len(grp) < 20:
                continue
            idx = df.index.get_indexer(grp.index)
            out["by_family"][str(family)] = {
                "rows": int(len(grp)),
                "base_rate": safe_float(y[idx].mean()),
                "auc": maybe_auc(y[idx], score[idx]),
                "q90": threshold_metrics(y[idx], score[idx], 0.90),
            }
    if "stage_hours" in eval_cols and "stage_hours" in df.columns:
        for stage, grp in df.groupby("stage_hours"):
            if len(grp) < 20:
                continue
            idx = df.index.get_indexer(grp.index)
            out["by_stage"][str(stage)] = {
                "rows": int(len(grp)),
                "base_rate": safe_float(y[idx].mean()),
                "auc": maybe_auc(y[idx], score[idx]),
                "q90": threshold_metrics(y[idx], score[idx], 0.90),
            }
    return out


def threshold_metrics(y: np.ndarray, score: np.ndarray, quantile: float) -> dict[str, Any]:
    if len(score) == 0:
        return {"selected": 0, "precision": None, "recall": None, "threshold": None}
    threshold = float(np.quantile(score, quantile))
    selected = score >= threshold
    hits = int((selected & (y == 1)).sum())
    total = int(selected.sum())
    positives = int((y == 1).sum())
    return {
        "threshold": safe_float(threshold),
        "selected": total,
        "precision": safe_float(hits / total) if total else None,
        "recall": safe_float(hits / positives) if positives else None,
    }


def maybe_auc(y: np.ndarray, score: np.ndarray) -> float | None:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    return safe_float(roc_auc_score(y, score))


def maybe_ap(y: np.ndarray, score: np.ndarray) -> float | None:
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    return safe_float(average_precision_score(y, score))


def importance(model: lgb.LGBMClassifier, features: list[str]) -> list[dict[str, Any]]:
    gains = model.booster_.feature_importance(importance_type="gain")
    pairs = sorted(zip(features, gains), key=lambda x: x[1], reverse=True)
    return [{"feature": f, "gain": safe_float(g)} for f, g in pairs[:25]]


def hours_to_bars(hours: float) -> int:
    return int(round(float(hours) * HOUR_MS / BAR_MS))


def configure_interval(interval: str) -> None:
    global ACTIVE_VARIANT, BAR_MS
    if interval == "15m":
        ACTIVE_VARIANT = VARIANT_15M
        BAR_MS = 15 * 60_000
    elif interval == "5m":
        ACTIVE_VARIANT = VARIANT_5M
        BAR_MS = 5 * 60_000
    elif interval == "5m_scaled":
        ACTIVE_VARIANT = VARIANT_5M_SCALED
        BAR_MS = 5 * 60_000
    else:
        raise ValueError(interval)


def current_variant() -> Variant:
    return ACTIVE_VARIANT


def safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not np.isfinite(out):
        return None
    return round(out, 6)


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "# Lifecycle Model Experiment",
        "",
        "Experiment-only pipeline for long entry, dynamic family recognition, flat-long, and short-start models.",
        "",
        "## Dataset",
        "",
        f"- Data: {result['data_start']} to {result['data_end']}",
        f"- Symbols: {result['symbols']}",
        f"- Long entries: {result['long_entries']}",
        f"- State rows: {result['state_rows']}",
        f"- Linked historical pump events: {result['linked_events']}",
        f"- Model directory: `{result['model_dir']}`",
        "",
        "## Family Counts on Linked Entries",
        "",
        "| Family | Entries |",
        "|---|---:|",
    ]
    for family, count in sorted(result["family_counts"].items(), key=lambda x: x[1], reverse=True):
        lines.append(f"| {family} | {count} |")
    lines += ["", "## Core Models", ""]
    for key in ("long_pump_event", "long_start_quality", "flat_long", "short_start"):
        model = result["models"][key]
        hold = model["holdout"]
        q90 = hold["thresholds"]["q90"]
        q95 = hold["thresholds"]["q95"]
        lines += [
            f"### {key}",
            f"- Rows: {model['rows']} positives={model['positives']} base={pct(model['base_rate'])}",
            f"- Holdout AUC={num(hold['auc'])} AP={num(hold['ap'])} base={pct(hold['base_rate'])}",
            f"- q90 precision={pct(q90['precision'])} recall={pct(q90['recall'])} selected={q90['selected']}",
            f"- q95 precision={pct(q95['precision'])} recall={pct(q95['recall'])} selected={q95['selected']}",
            "",
        ]
    fam = result["models"]["family"]
    lines += [
        "### family",
        f"- Rows: {fam['rows']} events={fam['events']}",
        f"- Holdout accuracy={pct(fam['holdout']['accuracy'])} balanced={pct(fam['holdout']['balanced_accuracy'])}",
        "",
        "| Operational target | Base | AUC | q90 precision | q95 precision |",
        "|---|---:|---:|---:|---:|",
    ]
    for target, stats in fam["holdout"]["operational"].items():
        q90 = stats["thresholds"]["q90"]
        q95 = stats["thresholds"]["q95"]
        lines.append(
            f"| {target} | {pct(stats['base_rate'])} | {num(stats['auc'])} | "
            f"{pct(q90['precision'])} | {pct(q95['precision'])} |"
        )
    lines += [
        "",
        "## Expert Slices",
        "",
        "| Family | Target | Rows | Positives | Holdout AUC | q90 precision |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for family, targets in result["models"]["family_experts"].items():
        for target, stats in targets.items():
            if stats.get("skipped"):
                lines.append(f"| {family} | {target} | {stats['rows']} | {stats['positives']} | skipped | skipped |")
            else:
                q90 = stats["holdout"]["thresholds"]["q90"]
                lines.append(
                    f"| {family} | {target} | {stats['rows']} | {stats['positives']} | "
                    f"{num(stats['holdout']['auc'])} | {pct(q90['precision'])} |"
                )
    lines += [
        "",
        "## Notes",
        "",
        "- Labels use future paths; features use only closed bars at the decision time.",
        "- Family labels come from historical event taxonomy and are not direct live labels.",
        "- Production integration should first use these scores as signal tiers, not automatic orders.",
    ]
    return "\n".join(lines)


def pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.4f}"


if __name__ == "__main__":
    main()
