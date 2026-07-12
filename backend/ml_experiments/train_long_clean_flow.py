"""Train cleaner long-entry models with OI / long-short / taker flow features.

This is an offline research experiment. It uses the bb top200_15m local
database and keeps all labels strictly forward-looking while all features use
closed/current and historical rows only.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from pandas.errors import PerformanceWarning
from sklearn.metrics import average_precision_score, roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pump_dump_hunter.data.bb_importer import DEFAULT_NON_ALT_SYMBOLS
from pump_dump_hunter.ml import features as mlf


DAY_MS = 86_400_000
BAR_MS = 15 * 60_000
HORIZON_48H = 48 * 4
HORIZON_24H = 24 * 4
HORIZON_72H = 72 * 4
EARLY_2H = 2 * 4
EARLY_6H = 6 * 4
EARLY_12H = 12 * 4
EARLY_24H = 24 * 4

warnings.filterwarnings("ignore", category=PerformanceWarning)


FLOW_COLUMNS = [
    "funding_rate",
    "oi",
    "oi_value",
    "global_ls",
    "top_acct_ls",
    "top_pos_ls",
    "taker_ratio",
    "oi_chg_4",
    "oi_chg_16",
    "oi_chg_48",
    "oi_value_chg_16",
    "oi_price_div_16",
    "global_ls_chg_16",
    "top_acct_ls_chg_16",
    "top_pos_ls_chg_16",
    "top_pos_global_div",
    "top_acct_global_div",
    "top_pos_z_96",
    "top_acct_z_96",
    "global_ls_z_96",
    "taker_ratio_ma8",
    "taker_ratio_z_96",
    "funding_z_96",
]


LONG_EXTRA = [
    "qv30_rank",
    "ret30_rank",
    "qv30_rank_pct",
    "ret30_rank_pct",
    "qv30",
    "qv30_ratio",
    "body_break_8",
]

PROFILE_COLUMNS = [
    "profile_prod_breakout",
    "profile_early_breakout",
    "profile_trend_hold",
]


@dataclass(frozen=True)
class ExperimentResult:
    name: str
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
    source = Path(args.source)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = discover_symbols(source, args.max_symbols)
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        symbols = [s for s in symbols if s in wanted]
    print(f"symbols={len(symbols)}", flush=True)

    ranks = build_rank_frame(source, symbols, args.days)
    print(f"rank rows={len(ranks)}", flush=True)
    frames: list[pd.DataFrame] = []
    skipped: list[str] = []
    for i, symbol in enumerate(symbols, 1):
        try:
            frame = build_symbol_dataset(source, symbol, ranks, args.days)
        except Exception as exc:
            skipped.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:140]}")
            continue
        if frame.empty:
            skipped.append(f"{symbol}:empty")
            continue
        frames.append(frame)
        if i % 40 == 0:
            print(f"loaded {i}/{len(symbols)} rows={sum(len(x) for x in frames)}", flush=True)
    if not frames:
        raise SystemExit("no training rows")
    data = pd.concat(frames, ignore_index=True).sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    data = data.replace([np.inf, -np.inf], np.nan)
    data_path = out_dir / "long_clean_flow_dataset.parquet"
    data.to_parquet(data_path, index=False)

    feature_sets = {
        "price_only": mlf.feature_columns() + LONG_EXTRA + PROFILE_COLUMNS,
        "price_flow": mlf.feature_columns() + LONG_EXTRA + PROFILE_COLUMNS + FLOW_COLUMNS,
    }
    targets = ["y_old_long_start", "y_clean_24h", "y_clean_48h", "y_smooth_72h"]
    results: list[ExperimentResult] = []
    for target in targets:
        for feature_name, feature_cols in feature_sets.items():
            print(f"training {target}/{feature_name}", flush=True)
            results.append(train_one(data, target, feature_name, feature_cols, out_dir))

    payload = {
        "source": str(source),
        "dataset": str(data_path),
        "symbols_requested": len(symbols),
        "symbols_used": int(data["symbol"].nunique()),
        "rows": int(len(data)),
        "start_time": int(data["entry_time"].min()),
        "end_time": int(data["entry_time"].max()),
        "labels": {
            "y_old_long_start": "future_high_48h>=12% and adverse_before_up5<=8%",
            "y_clean_24h": "future_high_24h>=8%, adverse_before_up5<=2.5%, first_2h_adverse<=2%, first_6h_adverse<=3.5%",
            "y_clean_48h": "future_high_48h>=10%, adverse_before_up5<=3.5%, first_2h_adverse<=2.5%, first_6h_adverse<=4%",
            "y_smooth_72h": "future_high_72h>=12%, adverse_before_up5<=3.5%, first_6h_adverse<=4%, first_12h_adverse<=5%, first_24h_adverse<=6%",
        },
        "candidate": "closed 15m rows, three profiles: prod_breakout, early_breakout, trend_hold; 24h symbol cooldown; labels use future paths only as targets",
        "skipped_count": len(skipped),
        "skipped": skipped[:200],
        "results": [r.__dict__ for r in results],
    }
    (out_dir / "long_clean_flow_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "long_clean_flow_results.md").write_text(render_report(payload), encoding="utf-8")
    print(json.dumps({"out": str(out_dir), "rows": len(data), "symbols": data["symbol"].nunique()}, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train clean long-entry models with flow features.")
    parser.add_argument("--source", default=r"E:\A\bb\data\top200_15m")
    parser.add_argument("--out-dir", default="backend/storage/ml/long_clean_flow")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--symbols", default="")
    return parser.parse_args(argv)


def discover_symbols(source: Path, max_symbols: int) -> list[str]:
    klines = source / "klines"
    excluded = set(DEFAULT_NON_ALT_SYMBOLS)
    excluded |= {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT"}
    required_dirs = [
        source / "market_state_hist" / "oi",
        source / "market_state_hist" / "global_acct_ratio",
        source / "market_state_hist" / "top_acct_ratio",
        source / "market_state_hist" / "top_pos_ratio",
    ]
    symbols = []
    for path in sorted(klines.glob("*.parquet")):
        symbol = path.stem.upper()
        if symbol in excluded or not symbol.isascii() or not symbol.isalnum():
            continue
        if all((d / f"{symbol}.parquet").exists() for d in required_dirs):
            symbols.append(symbol)
    return symbols[:max_symbols] if max_symbols else symbols


def read_klines(path: Path, days: int) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    if days > 0 and not df.empty:
        start = int(df["timestamp"].max()) - days * DAY_MS + 1
        df = df[df["timestamp"] >= start].copy()
    df = df.rename(columns={"timestamp": "entry_time", "quote_volume": "qv", "taker_buy_quote_volume": "tbq"})
    return df[["entry_time", "open", "high", "low", "close", "qv", "tbq"]]


def build_rank_frame(source: Path, symbols: list[str], days: int) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        path = source / "klines" / f"{symbol}.parquet"
        try:
            k = read_klines(path, days)
        except Exception:
            continue
        if k.empty:
            continue
        qv30 = k["qv"].rolling(2, min_periods=2).sum()
        ret30 = k["close"] / k["close"].shift(2) - 1.0
        rows.append(pd.DataFrame({"symbol": symbol, "entry_time": k["entry_time"], "qv30": qv30, "ret30": ret30}))
    ranks = pd.concat(rows, ignore_index=True)
    ranks["qv30_rank"] = ranks.groupby("entry_time")["qv30"].rank(method="min", ascending=False)
    ranks["ret30_rank"] = ranks.groupby("entry_time")["ret30"].rank(method="min", ascending=False)
    counts = ranks.groupby("entry_time")["symbol"].transform("count")
    ranks["qv30_rank_pct"] = ranks["qv30_rank"] / counts
    ranks["ret30_rank_pct"] = ranks["ret30_rank"] / counts
    return ranks[["symbol", "entry_time", "qv30_rank", "ret30_rank", "qv30_rank_pct", "ret30_rank_pct"]]


def build_symbol_dataset(source: Path, symbol: str, ranks: pd.DataFrame, days: int) -> pd.DataFrame:
    k = read_klines(source / "klines" / f"{symbol}.parquet", days)
    if len(k) < 600:
        return pd.DataFrame()
    frame = k.rename(columns={"entry_time": "b"})
    feats = mlf.compute_features(frame)
    add_long_extras(frame, feats)
    flow = build_flow_features(source, symbol, frame)
    work = pd.concat([frame[["b", "open", "high", "low", "close", "qv", "tbq"]], feats, flow], axis=1)
    work = work.rename(columns={"b": "entry_time"})
    work["symbol"] = symbol
    work = work.merge(ranks[ranks["symbol"] == symbol].drop(columns=["symbol"]), on="entry_time", how="left")
    profiles = long_candidate_profiles(work)
    for name, mask in profiles.items():
        work[f"profile_{name}"] = mask.astype("int8")
    candidate = np.column_stack(list(profiles.values())).any(axis=1)
    valid = np.zeros(len(work), dtype=bool)
    valid[mlf.LOOKBACK : max(mlf.LOOKBACK, len(work) - HORIZON_72H - 2)] = True
    feature_cols = mlf.feature_columns() + LONG_EXTRA + PROFILE_COLUMNS + FLOW_COLUMNS
    finite_base = work[mlf.feature_columns() + LONG_EXTRA].notna().all(axis=1).to_numpy()
    positions = dedup_indices(np.flatnonzero(candidate & valid & finite_base), 96)
    if not positions:
        return pd.DataFrame()
    labels = build_labels(work)
    out = work.iloc[positions][["symbol", "entry_time", "open", "high", "low", "close"] + feature_cols].copy()
    out["candidate_profile"] = [profile_name_at(profiles, ix) for ix in positions]
    for col in labels:
        out[col] = labels[col].iloc[positions].values
    out["entry_price"] = work["close"].iloc[positions].values
    return out


def add_long_extras(frame: pd.DataFrame, feats: pd.DataFrame) -> None:
    qv30 = frame["qv"].rolling(2, min_periods=2).sum()
    feats["qv30"] = qv30
    feats["qv30_ratio"] = qv30 / qv30.shift(1).rolling(20, min_periods=10).mean()
    body_high = pd.concat([frame["open"], frame["close"]], axis=1).max(axis=1)
    feats["body_break_8"] = (frame["close"] > body_high.shift(1).rolling(8, min_periods=8).max()).astype("int8")


def build_flow_features(source: Path, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    base = pd.DataFrame({"entry_time": frame["b"].astype("int64"), "close": frame["close"].astype(float)})
    specs = [
        ("market_state_hist/oi", ["oi", "oi_value"]),
        ("market_state_hist/global_acct_ratio", ["global_ls"]),
        ("market_state_hist/top_acct_ratio", ["top_acct_ls"]),
        ("market_state_hist/top_pos_ratio", ["top_pos_ls"]),
        ("market_state_hist/taker_ratio", ["taker_ratio"]),
        ("funding", ["funding_rate"]),
    ]
    merged = base.copy()
    for folder, names in specs:
        path = source / folder / f"{symbol}.parquet"
        if not path.exists():
            for name in names:
                merged[name] = np.nan
            continue
        raw = pd.read_parquet(path).drop_duplicates("timestamp").sort_values("timestamp")
        if len(names) == 1:
            value_col = "ratio" if "ratio" in raw.columns else names[0]
            raw = raw[["timestamp", value_col]].rename(columns={value_col: names[0]})
        else:
            raw = raw[["timestamp"] + names]
        merged = pd.merge_asof(merged.sort_values("entry_time"), raw, left_on="entry_time", right_on="timestamp", direction="backward")
        if "timestamp" in merged:
            merged = merged.drop(columns=["timestamp"])
    out = pd.DataFrame(index=frame.index)
    for col in ("funding_rate", "oi", "oi_value", "global_ls", "top_acct_ls", "top_pos_ls", "taker_ratio"):
        out[col] = pd.to_numeric(merged.get(col), errors="coerce").to_numpy()
    out["oi_chg_4"] = out["oi"] / out["oi"].shift(4) - 1.0
    out["oi_chg_16"] = out["oi"] / out["oi"].shift(16) - 1.0
    out["oi_chg_48"] = out["oi"] / out["oi"].shift(48) - 1.0
    out["oi_value_chg_16"] = out["oi_value"] / out["oi_value"].shift(16) - 1.0
    ret16 = frame["close"] / frame["close"].shift(16) - 1.0
    out["oi_price_div_16"] = out["oi_chg_16"] - ret16
    for col in ("global_ls", "top_acct_ls", "top_pos_ls"):
        out[f"{col}_chg_16"] = out[col] / out[col].shift(16) - 1.0
    out["global_ls_z_96"] = zscore(out["global_ls"], 96)
    out["top_acct_z_96"] = zscore(out["top_acct_ls"], 96)
    out["top_pos_z_96"] = zscore(out["top_pos_ls"], 96)
    out["top_pos_global_div"] = out["top_pos_ls"] - out["global_ls"]
    out["top_acct_global_div"] = out["top_acct_ls"] - out["global_ls"]
    out["taker_ratio_ma8"] = out["taker_ratio"].rolling(8, min_periods=4).mean()
    out["taker_ratio_z_96"] = zscore(out["taker_ratio"], 96)
    out["funding_z_96"] = zscore(out["funding_rate"], 96)
    return out[FLOW_COLUMNS]


def zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=max(10, window // 4)).mean()
    std = series.rolling(window, min_periods=max(10, window // 4)).std().replace(0, np.nan)
    return (series - mean) / std


def long_candidate_profiles(rows: pd.DataFrame) -> dict[str, np.ndarray]:
    close = rows["close"]
    open_ = rows["open"]
    qv = rows["qv"]
    ret1 = close / close.shift(1) - 1.0
    ret2 = close / close.shift(2) - 1.0
    ret6 = close / close.shift(6) - 1.0
    ret12 = close / close.shift(12) - 1.0
    ret24 = close / close.shift(24) - 1.0
    ret16 = close / close.shift(16) - 1.0
    ret48 = close / close.shift(48) - 1.0
    ret96 = close / close.shift(96) - 1.0
    qv30 = qv.rolling(2, min_periods=2).sum()
    volr30 = qv30 / qv30.shift(1).rolling(20, min_periods=10).mean()
    body_high = pd.concat([open_, close], axis=1).max(axis=1)
    breakout = close > body_high.shift(1).rolling(8, min_periods=8).max()
    inpump = (ret16 >= mlf.PUMP_4H) | (ret48 >= mlf.PUMP_12H) | (ret96 >= mlf.PUMP_1D)
    common = (
        (rows["close_pos"] >= 0.55)
        & (rows["uwick"] <= 0.06)
        & (rows["dist_ema21"] > 0)
        & (rows["dist_ema21"] <= 0.12)
        & (rows["ema_spread"] > 0)
        & (rows["qv30_rank"] <= 200)
        & np.isfinite(rows["qv30_rank"])
        & (~inpump)
    )
    prod = (
        (ret2 >= mlf.LONG_RET2_MIN)
        & (volr30 >= mlf.LONG_VOLR30_MIN)
        & breakout
        & (ret96 <= mlf.LONG_HEAT_24H)
        & (ret16 <= mlf.LONG_HEAT_4H)
        & (ret48 <= mlf.LONG_HEAT_12H)
        & (rows["close_pos"] >= mlf.LONG_CPOS_MIN)
        & (rows["uwick"] <= mlf.LONG_UWICK_MAX)
        & (rows["dist_ema21"] > 0)
        & (rows["dist_ema21"] <= mlf.LONG_DIST_EMA21_MAX)
        & (rows["ema_spread"] > 0)
        & (rows["qv30_rank"] <= 150)
        & (~inpump)
    )
    early = (
        common
        & (ret2 >= 0.025)
        & (ret6 >= 0.035)
        & (volr30 >= 1.35)
        & breakout
        & (ret96 <= 0.22)
        & (ret16 <= 0.14)
        & (ret48 <= 0.24)
        & (rows["close_pos"] >= 0.62)
        & (rows["dist_ema21"] <= 0.08)
    )
    trend_hold = (
        common
        & (ret1 >= -0.005)
        & (ret2 >= 0.010)
        & (ret6 >= 0.025)
        & (ret12 >= 0.040)
        & (ret24 >= 0.050)
        & (ret96 <= 0.25)
        & (rows["dd_24"] >= -0.05)
        & (volr30 >= 0.95)
        & (rows["qv30_ratio"] >= 0.90)
        & (rows["dist_ema21"] <= 0.075)
        & (rows["close_pos"] >= 0.58)
        & (rows["body"] >= -0.01)
        & (rows["tsell_ma8"] <= 0.58)
    )
    return {
        "prod_breakout": prod.fillna(False).to_numpy(dtype=bool),
        "early_breakout": early.fillna(False).to_numpy(dtype=bool),
        "trend_hold": trend_hold.fillna(False).to_numpy(dtype=bool),
    }


def profile_name_at(profiles: dict[str, np.ndarray], ix: int) -> str:
    for name, mask in profiles.items():
        if bool(mask[ix]):
            return name
    return "unknown"


def build_labels(rows: pd.DataFrame) -> pd.DataFrame:
    close = rows["close"].to_numpy(dtype=float)
    high = rows["high"].to_numpy(dtype=float)
    low = rows["low"].to_numpy(dtype=float)
    fut_high_24 = future_max(high, HORIZON_24H) / close - 1.0
    fut_high_48 = future_max(high, HORIZON_48H) / close - 1.0
    fut_high_72 = future_max(high, HORIZON_72H) / close - 1.0
    adverse_before_up5, minutes_to_up5 = adverse_before_threshold(close, high, low, threshold=0.05, horizon=HORIZON_72H)
    adverse_before_up10, minutes_to_up10 = adverse_before_threshold(close, high, low, threshold=0.10, horizon=HORIZON_72H)
    first_2h_adverse = close / future_min(low, EARLY_2H) - 1.0
    first_6h_adverse = close / future_min(low, EARLY_6H) - 1.0
    first_12h_adverse = close / future_min(low, EARLY_12H) - 1.0
    first_24h_adverse = close / future_min(low, EARLY_24H) - 1.0
    out = pd.DataFrame(index=rows.index)
    out["future_high_24h"] = fut_high_24
    out["future_high_48h"] = fut_high_48
    out["future_high_72h"] = fut_high_72
    out["adverse_before_up5"] = adverse_before_up5
    out["adverse_before_up10"] = adverse_before_up10
    out["minutes_to_up5"] = minutes_to_up5
    out["minutes_to_up10"] = minutes_to_up10
    out["first_2h_adverse"] = first_2h_adverse
    out["first_6h_adverse"] = first_6h_adverse
    out["first_12h_adverse"] = first_12h_adverse
    out["first_24h_adverse"] = first_24h_adverse
    out["y_old_long_start"] = ((fut_high_48 >= 0.12) & (adverse_before_up5 <= 0.08)).astype("int8")
    out["y_clean_24h"] = (
        (fut_high_24 >= 0.08)
        & (adverse_before_up5 <= 0.025)
        & (first_2h_adverse <= 0.020)
        & (first_6h_adverse <= 0.035)
    ).astype("int8")
    out["y_clean_48h"] = (
        (fut_high_48 >= 0.10)
        & (adverse_before_up5 <= 0.035)
        & (first_2h_adverse <= 0.025)
        & (first_6h_adverse <= 0.04)
    ).astype("int8")
    out["y_smooth_72h"] = (
        (fut_high_72 >= 0.12)
        & (adverse_before_up5 <= 0.035)
        & (first_6h_adverse <= 0.040)
        & (first_12h_adverse <= 0.050)
        & (first_24h_adverse <= 0.060)
    ).astype("int8")
    return out


def future_max(values: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) <= horizon:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values[1:], horizon)
    out[: len(windows)] = windows.max(axis=1)
    return out


def future_min(values: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) <= horizon:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values[1:], horizon)
    out[: len(windows)] = windows.min(axis=1)
    return out


def adverse_before_threshold(close: np.ndarray, high: np.ndarray, low: np.ndarray, threshold: float, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    adverse = np.full(len(close), np.nan)
    minutes = np.full(len(close), np.nan)
    for i in range(0, max(0, len(close) - horizon - 1)):
        future_high = high[i + 1 : i + horizon + 1]
        hits = np.flatnonzero(future_high >= close[i] * (1.0 + threshold))
        if len(hits):
            end = i + int(hits[0]) + 2
            minutes[i] = (end - i - 1) * 15
        else:
            end = i + horizon + 1
        adverse[i] = close[i] / np.min(low[i + 1 : end]) - 1.0
    return adverse, minutes


def dedup_indices(indices: np.ndarray, cooldown_bars: int) -> list[int]:
    out: list[int] = []
    last = -10**9
    for ix in indices:
        if int(ix) - last >= cooldown_bars:
            out.append(int(ix))
            last = int(ix)
    return out


def train_one(data: pd.DataFrame, target: str, feature_set: str, feature_cols: list[str], out_dir: Path) -> ExperimentResult:
    cols = [c for c in feature_cols if c in data.columns]
    work = data.dropna(subset=[target]).copy()
    split = split_masks(work)
    train = work[split["train"]]
    val = work[split["val"]]
    hold = work[split["holdout"]]
    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=450,
        learning_rate=0.035,
        num_leaves=31,
        min_child_samples=40,
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
    thresholds = {f"q{int(q * 100)}": float(np.quantile(val_score, q)) for q in (0.80, 0.85, 0.90, 0.95, 0.98)}
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
    return ExperimentResult(
        name=f"{target}_{feature_set}",
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
    unique_times = np.sort(rows["entry_time"].unique())
    q70, q85 = np.quantile(unique_times, [0.70, 0.85])
    embargo = 3 * DAY_MS
    t = rows["entry_time"]
    return {
        "train": t < q70,
        "val": (t >= q70 + embargo) & (t <= q85),
        "holdout": t >= q85 + embargo,
    }


def threshold_metrics(rows: pd.DataFrame, score: np.ndarray, target: str, threshold: float) -> dict[str, Any]:
    selected = rows[score >= threshold]
    if selected.empty:
        return {"signals": 0}
    return {
        "signals": int(len(selected)),
        "precision": mean(selected[target]),
        "old_long_start_rate": mean(selected["y_old_long_start"]),
        "clean_24h_rate": mean(selected["y_clean_24h"]),
        "clean_48h_rate": mean(selected["y_clean_48h"]),
        "smooth_72h_rate": mean(selected["y_smooth_72h"]),
        "median_future_high_24h": median(selected["future_high_24h"]),
        "median_future_high_48h": median(selected["future_high_48h"]),
        "median_future_high_72h": median(selected["future_high_72h"]),
        "median_adverse_before_up5": median(selected["adverse_before_up5"]),
        "median_first_2h_adverse": median(selected["first_2h_adverse"]),
        "median_first_6h_adverse": median(selected["first_6h_adverse"]),
        "median_first_12h_adverse": median(selected["first_12h_adverse"]),
        "median_first_24h_adverse": median(selected["first_24h_adverse"]),
        "median_minutes_to_up5": median(selected["minutes_to_up5"]),
        "symbols": int(selected["symbol"].nunique()),
        "profiles": {str(k): int(v) for k, v in selected["candidate_profile"].value_counts().sort_index().items()},
    }


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


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Clean Long Flow Experiment",
        "",
        f"- Source: `{payload['source']}`",
        f"- Symbols used: {payload['symbols_used']}",
        f"- Rows: {payload['rows']}",
        f"- Candidate: {payload['candidate']}",
        "",
        "## Model Comparison",
        "",
        "| model | rows | holdout | AUC | AP | q90 sig | q90 clean24 | q90 clean48 | q90 smooth72 | q90 fut48 | q90 advUp5 | q90 first6h | q95 sig | q95 clean24 | q95 clean48 | q95 smooth72 | q95 fut72 | q95 advUp5 | q95 profiles |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in payload["results"]:
        q90 = result["threshold_metrics"].get("q90", {})
        q95 = result["threshold_metrics"].get("q95", {})
        lines.append(
            "| {name} | {rows} | {holdout} | {auc} | {ap} | {q90n} | {q90c24} | {q90c48} | {q90s72} | {q90f48} | {q90a} | {q90a6} | {q95n} | {q95c24} | {q95c48} | {q95s72} | {q95f72} | {q95a} | {q95p} |".format(
                name=result["name"],
                rows=result["rows"],
                holdout=result["holdout_rows"],
                auc=fmt_num(result["auc"]),
                ap=fmt_num(result["ap"]),
                q90n=q90.get("signals", 0),
                q90c24=fmt_pct(q90.get("clean_24h_rate")),
                q90c48=fmt_pct(q90.get("clean_48h_rate")),
                q90s72=fmt_pct(q90.get("smooth_72h_rate")),
                q90f48=fmt_pct(q90.get("median_future_high_48h")),
                q90a=fmt_pct(q90.get("median_adverse_before_up5")),
                q90a6=fmt_pct(q90.get("median_first_6h_adverse")),
                q95n=q95.get("signals", 0),
                q95c24=fmt_pct(q95.get("clean_24h_rate")),
                q95c48=fmt_pct(q95.get("clean_48h_rate")),
                q95s72=fmt_pct(q95.get("smooth_72h_rate")),
                q95f72=fmt_pct(q95.get("median_future_high_72h")),
                q95a=fmt_pct(q95.get("median_adverse_before_up5")),
                q95p=fmt_profiles(q95.get("profiles")),
            )
        )
    lines += ["", "## Top Flow Importances", ""]
    for result in payload["results"]:
        if result["feature_set"] != "price_flow":
            continue
        flow_top = [x for x in result["top_importance"] if x["feature"] in FLOW_COLUMNS][:12]
        lines += [f"### {result['name']}", ""]
        if not flow_top:
            lines.append("- No flow feature in top importances.")
        else:
            for item in flow_top:
                lines.append(f"- `{item['feature']}`: {item['importance']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.4f}"


def fmt_profiles(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    return ", ".join(f"{k}:{v}" for k, v in value.items())


if __name__ == "__main__":
    raise SystemExit(main())
