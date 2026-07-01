"""训练与实盘共用的特征工程。

关键约束:训练(train.py 读 parquet)和实盘(SignalEngine 读 15m 缓冲)必须调用
同一份 `compute_features`,否则模型在实盘上无效。所有特征只用已收线/过去数据。
"""
from __future__ import annotations

from typing import Any

try:
    import numpy as np
    import pandas as pd
except Exception as exc:  # pragma: no cover
    raise RuntimeError("ml.features 需要 numpy/pandas: pip install -r requirements.txt") from exc

N_RAW = 8  # 最近 N 根原始展平
LOOKBACK = 96  # 特征所需最少历史根数

# setup 候选判定阈值(训练与实盘一致)
PUMP_RUNUP = 0.20     # 近 96 根(24h)涨幅 >= 20% 才算盯盘态
TOP_DD24 = -0.04      # 距 24h 高点 >= -4%(近高位)
TOP_UWICK = 0.008     # 上影 >= 0.8%
TOP_CPOS = 0.5        # 收盘位置 <= 0.5
DUMP_DD96 = -0.04     # 距 96 根高点 <= -4%(已离开高位)
DUMP_CPOS = 0.4       # 弱收 <= 0.4


def compute_features(df: "pd.DataFrame") -> "pd.DataFrame":
    """输入 15m K 线(列: open/high/low/close/qv/tbq, 按时间升序), 返回逐根特征表。"""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    qv, tbq = df["qv"], df["tbq"]
    rng = (h - l).replace(0, np.nan)
    ret1 = c / c.shift(1) - 1
    close_pos = (c - l) / rng
    body = (c - o) / o
    uwick = (h - np.maximum(o, c)) / c
    lwick = (np.minimum(o, c) - l) / c
    volr20 = qv / qv.rolling(20).mean()
    tsell = 1 - tbq / qv.replace(0, np.nan)
    ema8 = c.ewm(span=8, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    d = pd.DataFrame(index=df.index)
    for k in (1, 2, 3, 6, 12, 24, 48, 96):
        d[f"ret_{k}"] = c / c.shift(k) - 1
    for k in (8, 24, 96):
        d[f"dd_{k}"] = c / h.rolling(k).max() - 1
    for k in (24, 96):
        d[f"runup_{k}"] = c / l.rolling(k).min() - 1
    d["volr_20"] = volr20
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
    d["new_high_96"] = (c >= h.rolling(96).max() * 0.999).astype("int8")
    d["consec"] = np.sign(body).rolling(3).sum()
    for lag in range(1, N_RAW + 1):
        d[f"r_ret_{lag}"] = ret1.shift(lag)
        d[f"r_cpos_{lag}"] = close_pos.shift(lag)
        d[f"r_body_{lag}"] = body.shift(lag)
        d[f"r_uw_{lag}"] = uwick.shift(lag)
        d[f"r_lw_{lag}"] = lwick.shift(lag)
        d[f"r_volr_{lag}"] = volr20.shift(lag)
        d[f"r_ts_{lag}"] = tsell.shift(lag)
    return d


def feature_columns() -> list[str]:
    """基础特征列的规范顺序(与 compute_features 输出一致)。"""
    cols: list[str] = []
    for k in (1, 2, 3, 6, 12, 24, 48, 96):
        cols.append(f"ret_{k}")
    cols += ["dd_8", "dd_24", "dd_96", "runup_24", "runup_96", "volr_20", "volr_48",
             "tsell", "tsell_ma8", "close_pos", "body", "uwick", "lwick",
             "retstd_20", "atr_14", "dist_ema8", "dist_ema21", "ema_spread",
             "accel", "new_high_96", "consec"]
    for lag in range(1, N_RAW + 1):
        cols += [f"r_ret_{lag}", f"r_cpos_{lag}", f"r_body_{lag}", f"r_uw_{lag}",
                 f"r_lw_{lag}", f"r_volr_{lag}", f"r_ts_{lag}"]
    return cols


def top_setup_flags(f: "pd.DataFrame | pd.Series"):
    return (f["runup_96"] >= PUMP_RUNUP) & (f["dd_24"] >= TOP_DD24) & (f["close_pos"] <= TOP_CPOS) & (f["uwick"] >= TOP_UWICK)


def dump_setup_flags(f: "pd.DataFrame | pd.Series"):
    return (f["runup_96"] >= PUMP_RUNUP) & (f["dd_96"] <= DUMP_DD96) & (f["body"] < 0) & (f["close_pos"] <= DUMP_CPOS)


def candles_to_frame(candles: list[Any]) -> "pd.DataFrame":
    """把引擎缓冲里的 Candle 列表转成特征所需的 15m DataFrame(按时间升序)。"""
    rows = [
        {"open": c.open, "high": c.high, "low": c.low, "close": c.close,
         "qv": c.quote_volume, "tbq": c.taker_buy_quote}
        for c in sorted(candles, key=lambda c: c.open_time)
    ]
    return pd.DataFrame(rows)
