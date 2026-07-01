from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trades: int
    taker_buy_base: float = 0.0
    taker_buy_quote: float = 0.0

    @classmethod
    def from_binance_rest(cls, symbol: str, interval: str, row: list[Any]) -> "Candle":
        return cls(
            symbol=symbol.upper(),
            interval=interval,
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
            quote_volume=float(row[7]),
            trades=int(row[8]),
            taker_buy_base=float(row[9]),
            taker_buy_quote=float(row[10]),
        )

    @classmethod
    def from_ws_kline(cls, data: dict[str, Any]) -> "Candle":
        k = data["k"]
        return cls(
            symbol=str(k["s"]).upper(),
            interval=str(k["i"]),
            open_time=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            close_time=int(k["T"]),
            quote_volume=float(k["q"]),
            trades=int(k["n"]),
            taker_buy_base=float(k.get("V", 0.0)),
            taker_buy_quote=float(k.get("Q", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "close_time": self.close_time,
            "quote_volume": self.quote_volume,
            "trades": self.trades,
            "taker_buy_base": self.taker_buy_base,
            "taker_buy_quote": self.taker_buy_quote,
        }


@dataclass(frozen=True)
class KlineClosed:
    symbol: str
    interval: str
    candle: Candle
    received_time: int | None = None

    @property
    def decision_time(self) -> int:
        return self.candle.close_time


@dataclass(frozen=True)
class DiscoveryTick:
    timestamp: int
    data_cutoff_time: int


@dataclass
class LiquidityRecord:
    symbol: str
    rank: int
    last_price: float
    quote_volume_15m: float
    quote_volume_30m: float
    pct_15m: float
    pct_30m: float
    amp_15m: float
    amp_30m: float
    volume_ratio_15m: float
    volume_ratio_30m: float
    gain_rank_15m: int
    gain_rank_30m: int
    selected: bool
    pump_qualified: bool
    data_cutoff_time: int
    pct_4h: float = 0.0
    pct_12h: float = 0.0
    pct_1d: float = 0.0
    quote_volume_4h: float = 0.0
    quote_volume_12h: float = 0.0
    quote_volume_1d: float = 0.0
    qvol30_rank: int = 0          # 横截面 30m 成交额排名(做多候选用)
    long_candidate: bool = False  # 做多候选(动量+热度+排名粗筛, 结构+ML在引擎判)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SignalParams:
    pump_15m_pct: float = 8.0
    pump_30m_pct: float = 10.0
    pump_4h_pct: float = 20.0
    pump_12h_pct: float = 30.0
    pump_1d_pct: float = 40.0
    volume_ratio_15m: float = 2.0
    volume_ratio_30m: float = 2.0
    gain_rank_top: int = 10
    ranked_min_15m_pct: float = 5.0
    ranked_min_30m_pct: float = 8.0
    max_24h_pct: float = 10000.0
    early_drop_from_high_pct: float = 6.0
    early_1m_return_pct: float = 3.0
    early_volume_ratio: float = 2.0
    early_min_remaining_pct: float = 20.0
    rejection_upper_wick_pct: float = 3.0
    rejection_probe_pct: float = 1.0
    rejection_two_bar_drop_pct: float = 4.0
    consolidation_lookback: int = 3
    consolidation_max_range_pct: float = 10.0
    consolidation_max_drift_pct: float = 4.0
    close_back_inside_buffer_pct: float = 1.0
    confirm_drop_from_high_pct: float = 8.0
    confirm_volume_ratio: float = 2.5
    confirm_min_remaining_pct: float = 25.0
    new_high_reset_pct: float = 0.5
    # --- v2 信号(数据驱动): early=放量冲高回落, short=高位横盘破位, fallback=回落兜底 ---
    early_v2_vol_ratio: float = 2.0
    early_v2_close_pos_max: float = 0.45
    early_v2_near_high_pct: float = 3.0
    early_v2_min_remaining_pct: float = 10.0
    short_v2_break_pct: float = 7.0
    short_v2_close_pos_max: float = 0.35
    short_v2_vol_ratio: float = 1.2
    short_v2_taker_min: float = 0.0
    short_v2_min_remaining_pct: float = 5.0
    fallback_drop_pct: float = 8.0
    # --- 做多线(候选粗筛门槛, 与研究一致) ---
    long_ret_30m_pct: float = 4.5
    long_vol_ratio_30m: float = 2.0
    long_heat_24h_pct: float = 25.0
    long_heat_4h_pct: float = 18.0
    long_heat_12h_pct: float = 28.0
    long_qvol_rank_top: int = 150

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalParams":
        base = cls()
        values = base.__dict__.copy()
        values.update({k: v for k, v in data.items() if k in values})
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class PumpEvent:
    event_id: str
    symbol: str
    first_seen: int
    last_seen: int
    expires_at: int
    trigger_window: str
    anchor_price: float
    high_price: float
    high_time: int
    current_price: float
    max_gain_pct: float
    status: str = "active"
    evidence: list[str] = field(default_factory=list)
    early_alerted_after_high_time: int | None = None
    short_alerted_after_high_time: int | None = None
    fallback_alerted_after_high_time: int | None = None
    early_alert_seq: int = 0
    short_signal_seq: int = 0
    fallback_alert_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class LongEvent:
    """做多监管事件: 入选后保持 W 小时窗口, 期间可多次触发做多信号(第N次=信号增强)。
    与妖币(PumpEvent)可重叠;退出=见顶(平多)/趋势破坏-8%/W超时。"""
    event_id: str
    symbol: str
    first_seen: int
    last_seen: int
    expires_at: int          # first_seen + W(long_watch_hours)
    entry_price: float       # 首次入做多监管价(趋势破坏 -X% 基准)
    high_price: float        # 入选后最高价(展示/趋势破坏参照)
    current_price: float
    long_signal_seq: int = 0
    status: str = "active"
    exit_reason: str = ""    # "" 活跃 / "见顶" / "趋势破坏" / "超时"
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Alert:
    alert_id: str
    event_id: str
    symbol: str
    level: str
    decision_time: int
    source_candle_close_time: int
    data_cutoff_time: int
    price: float
    invalidation_price: float
    anchor_price: float
    high_price: float
    remaining_downside_pct: float
    volume_ratio: float
    evidence: list[str]
    risks: list[str]
    category: str = ""
    occurrence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
