"""Lifecycle expert feature engineering and dynamic routing.

This module is production code for the lifecycle-expert strategy. It mirrors
the research pipeline used for the selected setup:

- 5m native long-entry models;
- 15m dense lifecycle fast/slow expert models;
- dyn_big_pump_tolerant behavior router;
- per-high signal arming with cooldown configured in settings.

All inputs are closed candles only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


BASE_INTERVAL_MS = 15 * 60_000
HOUR_MS = 3_600_000
N_RAW = 8
LOOKBACK_UNITS = 96

BEHAVIOR_ORDER = [
    "acceleration",
    "climax_risk",
    "distribution",
    "breakdown",
    "pullback_risk",
    "trend_hold",
    "neutral_watch",
]
FAST_TOP_GATE = {"distribution", "climax_risk"}
SLOW_TOP_GATE = {"distribution", "climax_risk"}
FAST_SHORT_GATE = {"pullback_risk", "breakdown"}
SLOW_SHORT_GATE = {"breakdown"}
FAST_GATE = FAST_TOP_GATE
SLOW_GATE = SLOW_TOP_GATE

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

SLOW_DERIVED = [
    "slow_noise",
    "slow_amp",
    "slow_ret",
    "slow_drawdown",
    "slow_drawdown_over_amp",
    "slow_drawdown_over_noise",
    "slow_hours_log",
    "slow_range_pressure",
    "slow_sell_pressure",
    "slow_ret6_over_noise",
    "slow_dist21_over_noise",
    "slow_maturity",
]

HIGH_PUMP_ORIG_CONTEXT = [
    "orig_ctx_ret_since_entry",
    "orig_ctx_high_since_entry",
    "orig_ctx_low_since_entry",
    "orig_ctx_drawdown_from_entry_high",
    "orig_ctx_hours_since_entry",
    "high40_cross_orig_gain",
]

HIGH_PUMP_TOP_GATE = {"acceleration", "trend_hold", "climax_risk", "distribution"}
HIGH_PUMP_SHORT_GATE = {"pullback_risk", "breakdown", "distribution", "climax_risk"}


@dataclass(frozen=True)
class RouterConfig:
    min_high_floor: float = 0.10
    high_noise_mult: float = 2.0
    pull_amp: float = 0.14
    pull_noise: float = 1.3
    break_amp: float = 0.26
    break_noise: float = 2.0
    dist_min_amp: float = 0.050
    dist_min_noise: float = 0.8
    dist_max_amp: float = 0.36
    dist_max_noise: float = 2.3
    climax_min_amp: float = 0.14
    climax_noise: float = 1.2
    ret_noise: float = 1.2


def feature_columns() -> list[str]:
    cols: list[str] = []
    for k in (1, 2, 3, 6, 12, 24, 48, 96):
        cols.append(f"ret_{k}")
    cols += [
        "dd_8",
        "dd_24",
        "dd_96",
        "runup_24",
        "runup_96",
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
        "new_high_96",
        "consec",
    ]
    for lag in range(1, N_RAW + 1):
        cols += [f"r_ret_{lag}", f"r_cpos_{lag}", f"r_body_{lag}", f"r_uw_{lag}", f"r_lw_{lag}", f"r_volr_{lag}", f"r_ts_{lag}"]
    return cols


FEATS = feature_columns()
LONG_EXTRA = ["qv30_rank", "ret30_rank", "qv30_rank_pct", "ret30_rank_pct", "qv30", "qv30_ratio", "body_break_8"]
LONG_FEATURES = FEATS + LONG_EXTRA
FAST_FEATURES = FEATS + ENTRY_CONTEXT + [f"behavior_{x}" for x in BEHAVIOR_ORDER]
SLOW_FEATURES = FAST_FEATURES + SLOW_DERIVED
ROUTER_FEATURES = FEATS + ENTRY_CONTEXT
HIGH_PUMP_FEATURES = FAST_FEATURES + SLOW_DERIVED + HIGH_PUMP_ORIG_CONTEXT

FAMILY_ORDER = [
    "normal_reversal",
    "slow_distribution",
    "fast_dump",
    "second_distribution",
    "continuation",
]

DEFAULT_ROUTE_THRESHOLDS = {
    "fast_dump": 0.914496,
    "slow_distribution": 0.701967,
    "second_distribution": 0.72,
    "continuation": 0.18,
}
DEFAULT_ROUTE_MARGIN = 0.12


def route_from_probabilities(
    probs: dict[str, float],
    thresholds: dict[str, float] | None = None,
    margin_threshold: float = DEFAULT_ROUTE_MARGIN,
) -> dict[str, Any]:
    """Convert family probabilities into an abstaining production route."""
    thresholds = thresholds or DEFAULT_ROUTE_THRESHOLDS
    p_fast = float(probs.get("fast_dump", 0.0) or 0.0)
    p_slow = float(probs.get("slow_distribution", 0.0) or 0.0)
    p_second = float(probs.get("second_distribution", 0.0) or 0.0)
    p_cont = float(probs.get("continuation", 0.0) or 0.0)
    candidates = [
        ("fast_dump", p_fast, float(thresholds.get("fast_dump", DEFAULT_ROUTE_THRESHOLDS["fast_dump"]))),
        ("slow_distribution", p_slow + p_second, float(thresholds.get("slow_distribution", DEFAULT_ROUTE_THRESHOLDS["slow_distribution"]))),
        ("second_distribution", p_second, float(thresholds.get("second_distribution", DEFAULT_ROUTE_THRESHOLDS["second_distribution"]))),
        ("continuation", p_cont, float(thresholds.get("continuation", DEFAULT_ROUTE_THRESHOLDS["continuation"]))),
    ]
    ranked = sorted(candidates, key=lambda item: item[1], reverse=True)
    best_mode, best_score, best_threshold = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = best_score - second_score
    mode = "unknown"
    if best_score >= best_threshold and margin >= margin_threshold:
        mode = best_mode
    if mode == "slow_distribution" and p_second > p_slow and p_second >= float(thresholds.get("second_distribution", 0.72)):
        mode = "second_distribution"
        best_score = p_second
    return {
        "mode": mode,
        "candidate": best_mode,
        "confidence": float(best_score),
        "margin": float(margin),
        "probs": {k: float(probs.get(k, 0.0) or 0.0) for k in FAMILY_ORDER},
    }


def bars_15m_units(units: int | float, interval_ms: int) -> int:
    if BASE_INTERVAL_MS % interval_ms != 0:
        raise ValueError("interval must divide 15m")
    return max(1, int(round(float(units) * BASE_INTERVAL_MS / interval_ms)))


def hours_to_bars(hours: float, interval_ms: int) -> int:
    return max(1, int(round(hours * HOUR_MS / interval_ms)))


def candles_to_frame(candles: list[Any]) -> pd.DataFrame:
    rows = [
        {
            "b": int(c.open_time),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "qv": float(c.quote_volume),
            "tbq": float(c.taker_buy_quote),
        }
        for c in sorted(candles, key=lambda c: c.open_time)
    ]
    return pd.DataFrame(rows)


def compute_features(df: pd.DataFrame, interval_ms: int, raw_lag_mode: str = "native") -> pd.DataFrame:
    o, h, low, c = df["open"], df["high"], df["low"], df["close"]
    qv, tbq = df["qv"], df["tbq"]
    rng = (h - low).replace(0, np.nan)
    ret1_native = c / c.shift(1) - 1
    close_pos = (c - low) / rng
    body = (c - o) / o
    uwick = (h - np.maximum(o, c)) / c
    lwick = (np.minimum(o, c) - low) / c
    volr20 = qv / qv.rolling(bars_15m_units(20, interval_ms)).mean()
    tsell = 1 - tbq / qv.replace(0, np.nan)
    ema8 = c.ewm(span=bars_15m_units(8, interval_ms), adjust=False).mean()
    ema21 = c.ewm(span=bars_15m_units(21, interval_ms), adjust=False).mean()

    d = pd.DataFrame(index=df.index)
    for k in (1, 2, 3, 6, 12, 24, 48, 96):
        d[f"ret_{k}"] = c / c.shift(bars_15m_units(k, interval_ms)) - 1
    for k in (8, 24, 96):
        d[f"dd_{k}"] = c / h.rolling(bars_15m_units(k, interval_ms)).max() - 1
    for k in (24, 96):
        d[f"runup_{k}"] = c / low.rolling(bars_15m_units(k, interval_ms)).min() - 1
    d["volr_20"] = volr20
    d["volr_48"] = qv / qv.rolling(bars_15m_units(48, interval_ms)).mean()
    d["tsell"] = tsell
    d["tsell_ma8"] = tsell.rolling(bars_15m_units(8, interval_ms)).mean()
    d["close_pos"] = close_pos
    d["body"] = body
    d["uwick"] = uwick
    d["lwick"] = lwick
    d["retstd_20"] = ret1_native.rolling(bars_15m_units(20, interval_ms)).std()
    d["atr_14"] = ((h - low) / c).rolling(bars_15m_units(14, interval_ms)).mean()
    d["dist_ema8"] = c / ema8 - 1
    d["dist_ema21"] = c / ema21 - 1
    d["ema_spread"] = ema8 / ema21 - 1
    d["accel"] = (c / c.shift(bars_15m_units(3, interval_ms)) - 1) - (
        c.shift(bars_15m_units(3, interval_ms)) / c.shift(bars_15m_units(6, interval_ms)) - 1
    )
    d["new_high_96"] = (c >= h.rolling(bars_15m_units(96, interval_ms)).max() * 0.999).astype("int8")
    d["consec"] = np.sign(body).rolling(bars_15m_units(3, interval_ms)).sum()
    for lag in range(1, N_RAW + 1):
        raw_shift = lag if raw_lag_mode == "native" else bars_15m_units(lag, interval_ms)
        d[f"r_ret_{lag}"] = ret1_native.shift(raw_shift)
        d[f"r_cpos_{lag}"] = close_pos.shift(raw_shift)
        d[f"r_body_{lag}"] = body.shift(raw_shift)
        d[f"r_uw_{lag}"] = uwick.shift(raw_shift)
        d[f"r_lw_{lag}"] = lwick.shift(raw_shift)
        d[f"r_volr_{lag}"] = volr20.shift(raw_shift)
        d[f"r_ts_{lag}"] = tsell.shift(raw_shift)
    return d


def add_long_extras(frame: pd.DataFrame, features: pd.DataFrame, rank_values: dict[str, float], interval_ms: int) -> pd.Series:
    row = features.iloc[-1].copy()
    qv30 = frame["qv"].rolling(hours_to_bars(0.5, interval_ms)).sum()
    qv30_ratio = qv30 / qv30.shift(1).rolling(hours_to_bars(5.0, interval_ms)).mean()
    body_high = pd.concat([frame["open"], frame["close"]], axis=1).max(axis=1)
    body_break = (frame["close"] > body_high.shift(1).rolling(hours_to_bars(2.0, interval_ms)).max()).astype("int8")
    row["qv30"] = float(qv30.iloc[-1])
    row["qv30_ratio"] = float(qv30_ratio.iloc[-1])
    row["body_break_8"] = float(body_break.iloc[-1])
    for key in ("qv30_rank", "ret30_rank", "qv30_rank_pct", "ret30_rank_pct"):
        row[key] = float(rank_values.get(key, np.nan))
    return row


def find_entry_index(frame: pd.DataFrame, entry_time: int) -> int:
    times = frame["b"].to_numpy(dtype=np.int64)
    ix = int(np.searchsorted(times, int(entry_time), side="left"))
    return min(max(ix, 0), max(len(frame) - 1, 0))


def context_since_entry(frame: pd.DataFrame, entry_ix: int, ix: int, interval_ms: int, entry_price: float | None = None) -> dict[str, float]:
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    open_ = frame["open"].to_numpy(dtype=float)
    qv = frame["qv"].to_numpy(dtype=float)
    tbq = frame["tbq"].to_numpy(dtype=float)
    bars = max(0, ix - entry_ix)
    seg = slice(entry_ix, ix + 1)
    pre = slice(max(0, entry_ix - hours_to_bars(24, interval_ms)), entry_ix)
    entry_close = float(entry_price) if entry_price and entry_price > 0 else float(close[entry_ix])
    seg_high = float(np.max(high[seg]))
    seg_low = float(np.min(low[seg]))
    pre_qv_mean = float(np.nanmean(qv[pre])) if entry_ix > 0 else float("nan")
    if not np.isfinite(pre_qv_mean) or pre_qv_mean <= 0:
        pre_qv_mean = float(np.nanmean(qv[max(0, ix - hours_to_bars(24, interval_ms)) : ix + 1]))
    recent_start = max(entry_ix, ix - hours_to_bars(3.75, interval_ms))
    recent_qv_mean = float(np.nanmean(qv[recent_start : ix + 1]))
    tsell = 1.0 - tbq[seg] / np.where(qv[seg] == 0, np.nan, qv[seg])
    bodies = close[seg] - open_[seg]
    return {
        "ctx_bars_since_entry": float(bars),
        "ctx_hours_since_entry": float(bars * interval_ms / HOUR_MS),
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


def build_lifecycle_row(candles: list[Any], entry_time: int, entry_price: float, interval_ms: int) -> pd.Series | None:
    frame = candles_to_frame(candles)
    required = bars_15m_units(LOOKBACK_UNITS, interval_ms) + 2
    if len(frame) < required:
        return None
    features = compute_features(frame, interval_ms)
    if features.iloc[-1][FEATS].isna().any():
        return None
    ix = len(frame) - 1
    entry_ix = find_entry_index(frame, entry_time)
    row = features.iloc[ix][FEATS].copy()
    for key, value in context_since_entry(frame, entry_ix, ix, interval_ms, entry_price).items():
        row[key] = value
    state = assign_behavior_state(row)
    row["behavior_state"] = state
    for behavior in BEHAVIOR_ORDER:
        row[f"behavior_{behavior}"] = 1.0 if state == behavior else 0.0
    return add_slow_features(row)


def build_high_pump_row(
    candles: list[Any],
    entry_time: int,
    anchor_price: float,
    min_gain_pct: float,
    interval_ms: int,
) -> pd.Series | None:
    """Build a lifecycle row reset from the first high-pump threshold crossing.

    The original PumpWatch context is retained as ``orig_*`` features, while the
    normal ``ctx_*`` features are recomputed from the first closed candle whose
    high reaches ``anchor_price * (1 + min_gain_pct)``. This mirrors the
    high-pump expert training set and uses only closed candles available at the
    decision point.
    """
    frame = candles_to_frame(candles)
    required = bars_15m_units(LOOKBACK_UNITS, interval_ms) + 2
    if len(frame) < required or anchor_price <= 0:
        return None
    features = compute_features(frame, interval_ms)
    if features.iloc[-1][FEATS].isna().any():
        return None
    ix = len(frame) - 1
    original_entry_ix = find_entry_index(frame, entry_time)
    cross_ix = find_high_pump_crossing_index(frame, original_entry_ix, anchor_price, min_gain_pct)
    if cross_ix is None or cross_ix > ix:
        return None
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    reset_entry_price = float(close[cross_ix])
    if not np.isfinite(reset_entry_price) or reset_entry_price <= 0:
        return None

    row = features.iloc[ix][FEATS].copy()
    original_context = context_since_entry(frame, original_entry_ix, ix, interval_ms, anchor_price)
    reset_context = context_since_entry(frame, cross_ix, ix, interval_ms, reset_entry_price)
    for key, value in original_context.items():
        row[f"orig_{key}"] = value
    row["high40_cross_orig_gain"] = float(np.max(high[original_entry_ix : cross_ix + 1]) / anchor_price - 1.0)
    for key, value in reset_context.items():
        row[key] = value

    state = assign_behavior_state(row)
    row["behavior_state"] = state
    for behavior in BEHAVIOR_ORDER:
        row[f"behavior_{behavior}"] = 1.0 if state == behavior else 0.0
    return add_slow_features(row)


def find_high_pump_crossing_index(
    frame: pd.DataFrame,
    original_entry_ix: int,
    anchor_price: float,
    min_gain_pct: float,
) -> int | None:
    if anchor_price <= 0:
        return None
    high = frame["high"].to_numpy(dtype=float)
    threshold = anchor_price * (1.0 + float(min_gain_pct) / 100.0)
    candidates = np.flatnonzero(high[original_entry_ix:] >= threshold)
    if len(candidates) == 0:
        return None
    return int(original_entry_ix + candidates[0])


def high_pump_top_setup(row: pd.Series) -> bool:
    drawdown = max(-f(row, "ctx_drawdown_from_entry_high"), 0.0)
    orig_drawdown = max(-f(row, "orig_ctx_drawdown_from_entry_high"), 0.0)
    state = str(row.get("behavior_state", "neutral_watch") or "neutral_watch")
    return bool(
        state in HIGH_PUMP_TOP_GATE
        and drawdown <= 0.12
        and f(row, "ctx_ret_since_entry") >= -0.08
        and f(row, "ret_3") >= -0.06
        and orig_drawdown <= 0.22
    )


def high_pump_short_setup(row: pd.Series) -> bool:
    drawdown = max(-f(row, "ctx_drawdown_from_entry_high"), 0.0)
    orig_drawdown = max(-f(row, "orig_ctx_drawdown_from_entry_high"), 0.0)
    state = str(row.get("behavior_state", "neutral_watch") or "neutral_watch")
    weak = f(row, "ret_3") <= -0.025 or f(row, "ret_6") <= -0.040 or f(row, "dist_ema21") <= -0.020
    return bool(
        state in HIGH_PUMP_SHORT_GATE
        and ((drawdown >= 0.035) or (orig_drawdown >= 0.06))
        and weak
    )


def add_slow_features(row: pd.Series) -> pd.Series:
    noise = slow_noise(row)
    amp = max(float(row.get("ctx_high_since_entry", 0.0) or 0.0), 0.0)
    ret = float(row.get("ctx_ret_since_entry", 0.0) or 0.0)
    drawdown = max(-float(row.get("ctx_drawdown_from_entry_high", 0.0) or 0.0), 0.0)
    red = float(row.get("ctx_red_bar_share", 0.0) or 0.0)
    tsell = float(row.get("ctx_taker_sell_mean", 0.5) or 0.5)
    qv_recent = max(float(row.get("ctx_qv_recent_ratio", 1.0) or 1.0), 0.0)
    hours = max(float(row.get("ctx_hours_since_entry", 0.0) or 0.0), 0.0)
    row["slow_noise"] = noise
    row["slow_amp"] = amp
    row["slow_ret"] = ret
    row["slow_drawdown"] = drawdown
    row["slow_drawdown_over_amp"] = drawdown / max(amp, 0.03)
    row["slow_drawdown_over_noise"] = drawdown / max(noise, 0.006)
    row["slow_hours_log"] = float(np.log1p(hours))
    row["slow_sell_pressure"] = max(red - 0.45, 0.0) + max(tsell - 0.50, 0.0)
    row["slow_range_pressure"] = row["slow_sell_pressure"] * float(np.log1p(qv_recent)) * float(np.sqrt(max(drawdown, 0.0)))
    row["slow_ret6_over_noise"] = float(row.get("ret_6", 0.0) or 0.0) / max(noise, 0.006)
    row["slow_dist21_over_noise"] = float(row.get("dist_ema21", 0.0) or 0.0) / max(noise, 0.006)
    row["slow_maturity"] = float(np.log1p(hours) * np.log1p(max(amp * 10.0, 0.0)))
    return row


def slow_noise(row: pd.Series) -> float:
    atr = float(row.get("atr_14", 0.0) or 0.0)
    retstd = float(row.get("retstd_20", 0.0) or 0.0) * 1.5
    return float(np.clip(max(atr, retstd), 0.006, 0.10))


def assign_behavior_state(row: pd.Series, cfg: RouterConfig | None = None) -> str:
    cfg = cfg or RouterConfig()
    ret = f(row, "ctx_ret_since_entry")
    high = max(f(row, "ctx_high_since_entry"), 0.0)
    drawdown = max(-f(row, "ctx_drawdown_from_entry_high"), 0.0)
    qv_recent = f(row, "ctx_qv_recent_ratio", 1.0)
    tsell = f(row, "ctx_taker_sell_mean", 0.5)
    red = f(row, "ctx_red_bar_share")
    new_high = f(row, "ctx_new_high_since_entry") > 0.5
    ret1 = f(row, "ret_1")
    ret3 = f(row, "ret_3")
    ret6 = f(row, "ret_6")
    uwick = f(row, "uwick")
    close_pos = f(row, "close_pos", 0.5)
    dist_ema21 = f(row, "dist_ema21")
    volr20 = f(row, "volr_20", 1.0)
    noise = slow_noise(row)

    min_high = max(cfg.min_high_floor, cfg.high_noise_mult * noise)
    pull_min = float(np.clip(max(0.045, cfg.pull_amp * high + cfg.pull_noise * noise), 0.045, 0.18))
    break_min = float(np.clip(max(0.08, cfg.break_amp * high + cfg.break_noise * noise), 0.08, 0.28))
    dist_min = float(np.clip(max(0.025, cfg.dist_min_amp * high + cfg.dist_min_noise * noise), 0.025, 0.10))
    dist_max = float(np.clip(max(0.12, cfg.dist_max_amp * high + cfg.dist_max_noise * noise), 0.12, 0.34))
    climax_amp = max(cfg.climax_min_amp, cfg.climax_noise * noise)
    ret3_break = -max(0.020, cfg.ret_noise * noise)
    ret6_break = -max(0.035, 1.4 * cfg.ret_noise * noise)
    ret1_pull = -max(0.018, cfg.ret_noise * noise)
    ret3_pull = -max(0.028, 1.25 * cfg.ret_noise * noise)
    ema_break = -max(0.015, 0.8 * cfg.ret_noise * noise)
    uwick_min = max(0.018, 1.2 * noise)

    breakdown = (high >= min_high) and (drawdown >= break_min) and ((ret3 <= ret3_break) or (ret6 <= ret6_break) or (dist_ema21 <= ema_break))
    fast_pullback = (
        (high >= min_high)
        and (drawdown >= pull_min)
        and (drawdown < max(break_min, pull_min + 0.02))
        and ((ret1 <= ret1_pull) or (ret3 <= ret3_pull))
        and ((qv_recent >= 1.15) or (volr20 >= 1.30))
    )
    acceleration = (
        (ret >= max(0.045, 1.8 * noise))
        and (drawdown <= max(0.035, 1.2 * noise))
        and ((new_high and (ret3 >= -0.2 * noise)) or (ret6 >= max(0.025, 1.2 * noise)))
        and (close_pos >= 0.55)
    )
    climax = (
        (high >= climax_amp)
        and (drawdown <= max(0.075, 2.0 * noise))
        and ((uwick >= uwick_min) or ((ret1 <= -0.8 * noise) and (qv_recent >= 1.10)) or ((close_pos <= 0.45) and (volr20 >= 1.45)))
    )
    distribution = (
        (high >= min_high)
        and (drawdown >= dist_min)
        and (drawdown < dist_max)
        and (ret >= -max(0.04, 1.5 * noise))
        and ((red >= 0.46) or (tsell >= 0.51) or (qv_recent >= 1.35))
    )
    trend_hold = (
        (ret >= max(0.045, 1.8 * noise))
        and (drawdown <= max(0.055, 1.6 * noise))
        and (ret6 >= -max(0.018, 0.8 * noise))
        and (dist_ema21 >= -max(0.012, 0.6 * noise))
    )

    state = "neutral_watch"
    if trend_hold:
        state = "trend_hold"
    if distribution:
        state = "distribution"
    if climax:
        state = "climax_risk"
    if acceleration:
        state = "acceleration"
    if fast_pullback:
        state = "pullback_risk"
    if breakdown:
        state = "breakdown"
    return state


def f(row: pd.Series, key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default))
    except Exception:
        return default
    return value if np.isfinite(value) else default


def finite_for(row: pd.Series, cols: list[str]) -> bool:
    for col in cols:
        try:
            value = float(row.get(col, np.nan))
        except Exception:
            return False
        if not np.isfinite(value):
            return False
    return True
