from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .config import ensure_dirs
from .data.rest_client import BinanceRestClient
from .data.store import Store
from .data.websocket_source import WebSocketMarketSource
from .discovery import build_broad_universe
from .models import Candle, KlineClosed
from .timeutils import closed_candle_cutoff_ms, iso_from_ms, local_day_from_ms, local_stamp, parse_duration_seconds, utc_ms


MINUTE_MS = 60_000
DEFAULT_FEE_RATE = 0.0008


@dataclass(frozen=True)
class WaterfallRule:
    name: str
    family: str
    exit_profile: str
    min_qv30: float
    min_body_drop: float
    min_2m_drop: float
    min_5m_drop: float
    min_volr20: float
    min_volr5_20: float
    min_tsell: float
    max_close_pos: float
    min_upper_wick: float
    break_lookback: int
    break_buffer: float
    min_ret_30m: float = -9.0
    max_ret_30m: float = 9.0
    min_ret_2h: float = -9.0
    max_ret_2h: float = 9.0
    min_ret_4h: float = -9.0
    max_ret_4h: float = 9.0
    min_ret_12h: float = -9.0
    max_ret_12h: float = 9.0
    min_ret_24h: float = -9.0
    max_ret_24h: float = 9.0
    min_drop_5m_entry: float = 0.0
    min_runup_24h: float = -9.0
    max_runup_24h: float = 9.0
    min_dd_from_24h_high: float = 0.0
    max_dd_from_24h_high: float = 9.0
    min_qv_over_prev6max: float = 0.0
    max_qv_over_prev6max: float = 999.0
    min_red_streak: int = 0
    min_lower_wick: float = 0.0
    max_lower_wick: float = 9.0
    min_range_pct: float = 0.0
    max_range_pct: float = 9.0
    max_volr20: float = 999.0
    max_volr5_20: float = 999.0


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_cap: float
    stop_body_high_buffer: float
    trail_activate: float
    trail_rebound: float
    quick_reclaim_buffer: float
    rebound_activate: float
    rebound_retrace: float
    max_hold_min: int


@dataclass
class WaterfallPosition:
    position_id: str
    symbol: str
    strategy: str
    family: str
    rule: str
    exit_profile: str
    status: str
    side: str
    entry_time: int
    entry_price: float
    notional_usdt: float
    stop_price: float
    best_price: float
    worst_price: float
    trail_price: float
    exit_time: int | None = None
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    exit_reason: str = ""
    fee_rate: float = DEFAULT_FEE_RATE
    margin_usdt: float = 0.0
    leverage: float = 1.0
    capital_fraction: float = 0.0
    evidence: list[str] = field(default_factory=list)
    updated_time: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WaterfallSignal:
    signal_id: str
    position_id: str
    symbol: str
    strategy: str
    action: str
    family: str
    rule: str
    decision_time: int
    price: float
    stop_price: float
    pnl_pct: float = 0.0
    confidence: float = 0.0
    tier: str = "normal"
    notional_usdt: float = 0.0
    margin_usdt: float = 0.0
    leverage: float = 1.0
    account_equity_usdt: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def waterfall_settings(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(settings.get("waterfall_quant") or {})
    cfg.setdefault("variant", "core5_agg")
    cfg.setdefault("broad_top", 450)
    cfg.setdefault("min_24h_quote_volume", settings.get("universe", {}).get("min_24h_quote_volume", 1_000_000))
    cfg.setdefault("discover_every", "15m")
    cfg.setdefault("prewarm_limit", 1500)
    cfg.setdefault("max_workers", 8)
    cfg.setdefault("watch_interval", "1m")
    cfg.setdefault("notional_usdt", 100.0)
    cfg.setdefault("paper_initial_balance_usdt", 100.0)
    cfg.setdefault("paper_margin_fraction", 0.20)
    cfg.setdefault("leverage", 10.0)
    cfg.setdefault("max_open_positions", 5)
    cfg.setdefault("fee_rate", DEFAULT_FEE_RATE)
    cfg.setdefault("same_symbol_cooldown_hours", 4.0)
    cfg.setdefault("after_stop_cooldown_hours", 6.0)
    cfg.setdefault("family_gap_minutes", 120)
    cfg.setdefault("max_trades_per_symbol_day", 2)
    cfg.setdefault("push_wecom", True)
    cfg.setdefault("enabled_families", ["post_pump", "downtrend_continuation", "other"])
    cfg.setdefault("execution_mode", "paper")
    cfg.setdefault("real_order_enabled", False)
    cfg.setdefault("micro_streams", ["aggTrade"])
    cfg.setdefault("require_agg_confirmation", True)
    cfg.setdefault("agg_sell_ratio_min", 0.60)
    cfg.setdefault("agg_low_time_frac_min", 0.55)
    cfg.setdefault("strong_agg_sell_ratio_min", 0.64)
    cfg.setdefault("strong_agg_close_pos_max", 0.15)
    cfg.setdefault("downtrend_agg_sell_ratio_min", 0.64)
    cfg.setdefault("downtrend_low_time_frac_min", 0.80)
    cfg.setdefault("momentum_low_time_frac_min", 0.80)
    cfg.setdefault("momentum_close_pos_max", 0.25)
    cfg.setdefault("store_micro_events", False)
    return cfg


def exit_profiles() -> dict[str, ExitProfile]:
    return {
        "medium_30_lock": ExitProfile("medium_30_lock", 0.030, 0.0030, 0.030, 0.010, 0.0030, 0.028, 0.018, 180),
        "medium_28_lock": ExitProfile("medium_28_lock", 0.028, 0.0030, 0.028, 0.009, 0.0025, 0.025, 0.016, 180),
        "dynamic_step_like": ExitProfile("dynamic_step_like", 0.030, 0.0030, 0.035, 0.010, 0.0025, 0.025, 0.014, 240),
        "let_big_run": ExitProfile("let_big_run", 0.035, 0.0040, 0.050, 0.015, 0.0040, 0.040, 0.026, 360),
    }


class MicroCutoffStats:
    def __init__(self) -> None:
        self.quote = 0.0
        self.sell_quote = 0.0
        self.high = 0.0
        self.low = 0.0
        self.last = 0.0
        self.low_time_ms = 0
        self.trades = 0

    def add(self, price: float, quote: float, sell_quote: float, offset_ms: int) -> None:
        if price <= 0 or quote <= 0:
            return
        self.quote += quote
        self.sell_quote += sell_quote
        self.trades += 1
        if self.high <= 0 or price > self.high:
            self.high = price
        if self.low <= 0 or price < self.low:
            self.low = price
            self.low_time_ms = max(0, offset_ms)
        self.last = price

    def snapshot(self, cutoff_ms: int) -> dict[str, float]:
        rng = self.high - self.low
        return {
            "quote": self.quote,
            "sell_ratio": safe_div(self.sell_quote, self.quote, 0.0),
            "close_pos": safe_div(self.last - self.low, rng, 0.5),
            "low_time_frac": safe_div(float(self.low_time_ms), float(max(1, cutoff_ms)), 0.0),
            "trades": float(self.trades),
        }


class WaterfallMicroState:
    """Keeps only current/recent minute aggTrade summaries used by live filters."""

    def __init__(self, cutoffs_ms: tuple[int, ...] = (40_000, 50_000, 59_000), keep_minutes: int = 4):
        self.cutoffs_ms = tuple(sorted(cutoffs_ms))
        self.keep_minutes = keep_minutes
        self.buckets: dict[tuple[str, int], dict[int, MicroCutoffStats]] = {}
        self.latest_book: dict[str, dict[str, float]] = {}

    def on_event(self, row: dict[str, Any]) -> None:
        stream = str(row.get("stream") or "")
        symbol = str(row.get("symbol") or "").upper()
        payload = row.get("payload") or {}
        if not symbol:
            return
        if stream == "aggTrade":
            self._on_agg_trade(symbol, int(row.get("event_time") or 0), payload)
        elif stream == "bookTicker":
            self._on_book_ticker(symbol, int(row.get("event_time") or 0), payload)

    def _on_agg_trade(self, symbol: str, event_time: int, payload: dict[str, Any]) -> None:
        price = float(payload.get("p") or 0.0)
        qty = float(payload.get("q") or 0.0)
        trade_time = int(payload.get("T") or event_time or utc_ms())
        if price <= 0 or qty <= 0:
            return
        minute_open = (trade_time // MINUTE_MS) * MINUTE_MS
        offset = max(0, min(MINUTE_MS - 1, trade_time - minute_open))
        quote = price * qty
        sell_quote = quote if bool(payload.get("m")) else 0.0
        bucket = self.buckets.setdefault((symbol, minute_open), {cutoff: MicroCutoffStats() for cutoff in self.cutoffs_ms})
        for cutoff in self.cutoffs_ms:
            if offset <= cutoff:
                bucket[cutoff].add(price, quote, sell_quote, offset)
        self._prune(trade_time)

    def _on_book_ticker(self, symbol: str, event_time: int, payload: dict[str, Any]) -> None:
        bid = float(payload.get("b") or 0.0)
        bid_qty = float(payload.get("B") or 0.0)
        ask = float(payload.get("a") or 0.0)
        ask_qty = float(payload.get("A") or 0.0)
        if bid <= 0 or ask <= 0:
            return
        self.latest_book[symbol] = {
            "event_time": float(event_time or utc_ms()),
            "bid": bid,
            "bid_qty": bid_qty,
            "ask": ask,
            "ask_qty": ask_qty,
            "spread_pct": safe_div(ask - bid, (ask + bid) / 2.0, 0.0),
            "bid_ask_qty_ratio": safe_div(bid_qty, ask_qty, 0.0),
        }

    def features(self, symbol: str, close_time: int) -> dict[str, float]:
        minute_open = ((close_time - 1) // MINUTE_MS) * MINUTE_MS
        bucket = self.buckets.get((symbol.upper(), minute_open), {})
        out: dict[str, float] = {}
        for cutoff in self.cutoffs_ms:
            snap = bucket.get(cutoff).snapshot(cutoff) if cutoff in bucket else {}
            prefix = f"m0_{int(cutoff / 1000)}s"
            out[f"{prefix}_quote"] = float(snap.get("quote", 0.0))
            out[f"{prefix}_sell_ratio"] = float(snap.get("sell_ratio", 0.0))
            out[f"{prefix}_close_pos"] = float(snap.get("close_pos", 0.5))
            out[f"{prefix}_low_time_frac"] = float(snap.get("low_time_frac", 0.0))
            out[f"{prefix}_trades"] = float(snap.get("trades", 0.0))
        book = self.latest_book.get(symbol.upper(), {})
        out["book_spread_pct"] = float(book.get("spread_pct", 0.0))
        out["book_bid_ask_qty_ratio"] = float(book.get("bid_ask_qty_ratio", 0.0))
        return out

    def _prune(self, now_ms: int) -> None:
        cutoff = ((now_ms // MINUTE_MS) - self.keep_minutes) * MINUTE_MS
        stale = [key for key in self.buckets if key[1] < cutoff]
        for key in stale:
            self.buckets.pop(key, None)


def build_waterfall_rules(variant: str = "core") -> list[WaterfallRule]:
    rules = [
        WaterfallRule(
            "robust_post_pump_red_sell_1m",
            "post_pump",
            "medium_30_lock",
            80_000,
            0.008,
            0.014,
            0.022,
            2.8,
            1.6,
            0.595,
            0.35,
            0.0015,
            20,
            0.001,
            min_ret_24h=0.385,
            min_red_streak=2,
        ),
        WaterfallRule(
            "robust_downtrend_range_flush_1m",
            "downtrend_continuation",
            "medium_28_lock",
            80_000,
            0.008,
            0.014,
            0.022,
            2.8,
            2.0,
            0.56,
            0.36,
            0.0015,
            20,
            0.001,
            max_ret_4h=-0.17,
            min_range_pct=0.052,
            max_volr5_20=2.94,
        ),
        WaterfallRule(
            "robust_downtrend_upper_break_1m",
            "downtrend_continuation",
            "medium_28_lock",
            80_000,
            0.006,
            0.010,
            0.018,
            2.2,
            1.7,
            0.52,
            0.42,
            0.0043,
            20,
            0.001,
            max_ret_2h=-0.176,
            max_volr5_20=2.7,
        ),
        WaterfallRule(
            "robust_momentum_uptrend_dump_1m",
            "momentum_dump",
            "dynamic_step_like",
            80_000,
            0.006,
            0.010,
            0.018,
            2.2,
            1.7,
            0.52,
            0.42,
            0.0,
            20,
            0.001,
            min_ret_2h=0.064,
            min_ret_12h=0.010,
            max_qv_over_prev6max=3.05,
        ),
        WaterfallRule(
            "robust_other_pullback_dump_1m",
            "other",
            "let_big_run",
            80_000,
            0.008,
            0.014,
            0.022,
            2.8,
            2.0,
            0.56,
            0.35,
            0.0015,
            20,
            0.001,
            min_dd_from_24h_high=0.094,
            min_lower_wick=0.006,
            min_ret_4h=-0.029,
        ),
    ]
    if variant == "high_pf":
        return [r for r in rules if r.family in {"downtrend_continuation", "other"}]
    return rules


class WaterfallEngine:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.cfg = waterfall_settings(settings)
        self.strategy = f"waterfall_{self.cfg['variant']}_1m"
        self.rules = build_waterfall_rules(str(self.cfg["variant"]))
        enabled = {str(x) for x in self.cfg.get("enabled_families", []) if str(x)}
        if enabled:
            self.rules = [rule for rule in self.rules if rule.family in enabled]
        self.profiles = exit_profiles()
        self.maxlen = max(1600, int(self.cfg.get("prewarm_limit", 1500)) + 16)
        self.candles: dict[str, deque[Candle]] = {}
        self.positions: dict[str, WaterfallPosition] = {}
        self.last_signal_time: dict[str, int] = {}
        self.last_stop_time: dict[str, int] = {}
        self.last_family_time: dict[tuple[str, str], int] = {}
        self.trade_count_day: dict[tuple[str, str], int] = {}
        self.micro = WaterfallMicroState()
        self.realized_pnl_usdt = 0.0
        self.initial_balance_usdt = float(self.cfg["paper_initial_balance_usdt"])
        self.leverage = float(self.cfg["leverage"])
        self.margin_fraction = float(self.cfg["paper_margin_fraction"])

    def load_positions(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            pos = waterfall_position_from_row(row)
            if pos.status == "open":
                self.positions[pos.symbol] = pos

    def load_recent_state(self, positions: list[dict[str, Any]], signals: list[dict[str, Any]]) -> None:
        for row in positions:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            family = str(row.get("family") or "")
            entry_time = int(row.get("entry_time") or 0)
            status = str(row.get("status") or "")
            if entry_time:
                self.last_signal_time[symbol] = max(self.last_signal_time.get(symbol, 0), entry_time)
                if family:
                    self.last_family_time[(symbol, family)] = max(self.last_family_time.get((symbol, family), 0), entry_time)
                day = local_day_from_ms(entry_time)
                self.trade_count_day[(symbol, day)] = self.trade_count_day.get((symbol, day), 0) + 1
            if status == "closed" and str(row.get("exit_reason") or "").startswith("stop"):
                exit_time = int(row.get("exit_time") or row.get("updated_time") or 0)
                if exit_time:
                    self.last_stop_time[symbol] = max(self.last_stop_time.get(symbol, 0), exit_time)
            if status == "closed":
                self.realized_pnl_usdt += float(row.get("pnl_usdt") or 0.0)
        for row in signals:
            symbol = str(row.get("symbol") or "")
            action = str(row.get("action") or "")
            t = int(row.get("decision_time") or 0)
            if symbol and t and action == "open_short":
                self.last_signal_time[symbol] = max(self.last_signal_time.get(symbol, 0), t)

    def on_micro(self, row: dict[str, Any]) -> None:
        self.micro.on_event(row)

    def prime_candles(self, candles: list[Candle]) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        for candle in sorted(candles, key=lambda c: (c.symbol, c.open_time)):
            self._append(candle)
            feat = self.features(candle.symbol)
            if feat:
                changed.append(self.watch_row(candle.symbol, feat, candle.close_time))
        return changed

    def on_kline(self, event: KlineClosed) -> tuple[list[dict[str, Any]], list[WaterfallPosition], list[WaterfallSignal]]:
        if event.interval != "1m":
            return [], [], []
        candle = event.candle
        self._append(candle)
        changed_watch: list[dict[str, Any]] = []
        changed_positions: list[WaterfallPosition] = []
        signals: list[WaterfallSignal] = []

        pos = self.positions.get(candle.symbol)
        if pos:
            exit_signal = self.update_position(pos, candle)
            changed_positions.append(pos)
            if exit_signal:
                signals.append(exit_signal)
                self.positions.pop(candle.symbol, None)

        feat = self.features(candle.symbol)
        if feat:
            changed_watch.append(self.watch_row(candle.symbol, feat, candle.close_time))
        if candle.symbol not in self.positions and feat:
            entry = self.entry_signal(candle.symbol, feat, candle)
            if entry:
                position, signal = entry
                self.positions[candle.symbol] = position
                changed_positions.append(position)
                signals.append(signal)
        return changed_watch, changed_positions, signals

    def _append(self, candle: Candle) -> None:
        dq = self.candles.setdefault(candle.symbol, deque(maxlen=self.maxlen))
        if dq and dq[-1].open_time == candle.open_time:
            dq[-1] = candle
        elif not dq or candle.open_time > dq[-1].open_time:
            dq.append(candle)

    def features(self, symbol: str) -> dict[str, float] | None:
        values = list(self.candles.get(symbol, []))
        if len(values) < 241:
            return None
        c = values[-1]
        close = c.close
        if close <= 0:
            return None
        body_low = min(c.open, c.close)
        body_high = max(c.open, c.close)
        rng = c.high - c.low
        qv = c.quote_volume
        qv20 = sum(x.quote_volume for x in values[-20:])
        qv5 = sum(x.quote_volume for x in values[-5:])
        qv30 = sum(x.quote_volume for x in values[-30:])
        tbq5 = sum(x.taker_buy_quote for x in values[-5:])
        qv_prev6 = max((x.quote_volume for x in values[-7:-1]), default=0.0)
        feat: dict[str, float] = {
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "body_low": body_low,
            "body_high": body_high,
            "close_pos": safe_div(c.close - c.low, rng, 0.5),
            "body_drop": safe_div(c.open - c.close, c.open, 0.0),
            "upper_wick": safe_div(c.high - body_high, c.open, 0.0),
            "lower_wick": safe_div(body_low - c.low, c.open, 0.0),
            "range_pct": safe_div(c.high - c.low, c.open, 0.0),
            "drop_2m": 1.0 - safe_div(c.close, values[-3].close, 1.0) if len(values) >= 3 else 0.0,
            "drop_5m": 1.0 - safe_div(c.close, values[-6].close, 1.0) if len(values) >= 6 else 0.0,
            "ret_30m": safe_div(c.close, values[-31].close, 1.0) - 1.0 if len(values) >= 31 else 0.0,
            "ret_2h": safe_div(c.close, values[-121].close, 1.0) - 1.0 if len(values) >= 121 else 0.0,
            "ret_4h": safe_div(c.close, values[-241].close, 1.0) - 1.0 if len(values) >= 241 else 0.0,
            "ret_12h": safe_div(c.close, values[-721].close, 1.0) - 1.0 if len(values) >= 721 else 0.0,
            "ret_24h": safe_div(c.close, values[-1441].close, 1.0) - 1.0 if len(values) >= 1441 else 0.0,
            "qv30": qv30,
            "volr20": safe_div(qv, qv20 / 20.0, 0.0),
            "volr5_20": safe_div(qv5, qv20 / 4.0, 0.0),
            "tsell": 1.0 - safe_div(c.taker_buy_quote, qv, 0.5),
            "tsell5": 1.0 - safe_div(tbq5, qv5, 0.5),
            "qv_over_prev6max": safe_div(qv, qv_prev6, 0.0),
            "red_streak": float(red_streak(values)),
        }
        day = values[-1440:] if len(values) >= 1440 else values
        low24 = min(x.low for x in day)
        high24 = max(x.high for x in day)
        feat["runup_24h"] = safe_div(c.close, low24, 1.0) - 1.0
        feat["dd_from_24h_high"] = 1.0 - safe_div(c.close, high24, 1.0)
        for n in (8, 20, 40):
            prior = values[-n - 1 : -1] if len(values) >= n + 1 else values[:-1]
            feat[f"prior_body_low_{n}"] = min((min(x.open, x.close) for x in prior), default=c.close)
        feat["family_code"] = family_code(classify_family(feat))
        return feat

    def watch_row(self, symbol: str, feat: dict[str, float], now: int) -> dict[str, Any]:
        family = classify_family(feat)
        pos = self.positions.get(symbol)
        return {
            "symbol": symbol,
            "strategy": self.strategy,
            "status": "in_position" if pos else "watching",
            "family": family,
            "last_time": now,
            "last_price": feat["close"],
            "ret_30m": feat["ret_30m"],
            "ret_2h": feat["ret_2h"],
            "ret_4h": feat["ret_4h"],
            "ret_24h": feat["ret_24h"],
            "runup_24h": feat["runup_24h"],
            "dd_from_24h_high": feat["dd_from_24h_high"],
            "qv30": feat["qv30"],
            "volr20": feat["volr20"],
            "volr5_20": feat["volr5_20"],
            "tsell": feat["tsell"],
            "updated_time": now,
            "evidence": [
                f"family={family}",
                f"ret24={feat['ret_24h'] * 100:.2f}%",
                f"qv30={feat['qv30']:.0f}",
            ],
        }

    def entry_signal(self, symbol: str, feat: dict[str, float], candle: Candle) -> tuple[WaterfallPosition, WaterfallSignal] | None:
        family = classify_family(feat)
        now = candle.close_time
        if not self.cooldown_ok(symbol, family, now):
            return None
        if len(self.positions) >= int(self.cfg["max_open_positions"]):
            return None
        for rule in self.rules:
            if rule.family != family:
                continue
            if not signal_ok(feat, rule):
                continue
            micro_feat = self.micro.features(symbol, now)
            micro_decision = self.micro_signal_decision(family, micro_feat)
            if not micro_decision["ok"]:
                return None
            sizing = self.paper_sizing()
            if sizing["margin_usdt"] <= 0 or sizing["notional_usdt"] <= 0:
                return None
            profile = self.profiles[rule.exit_profile]
            recent = list(self.candles[symbol])[-rule.break_lookback:]
            recent_high = max((max(x.open, x.close) for x in recent), default=candle.high)
            entry = candle.close
            stop = min(max(candle.high, recent_high) * (1.0 + profile.stop_body_high_buffer), entry * (1.0 + profile.stop_cap))
            position_id = f"wf-{symbol}-{now}-{rule.name}"
            evidence = [
                f"family={family}",
                f"rule={rule.name}",
                f"profile={profile.name}",
                f"broken_level={feat[f'prior_body_low_{rule.break_lookback}']:.12g}",
                f"qv30={feat['qv30']:.0f}",
                f"volr20={feat['volr20']:.2f}",
                f"volr5_20={feat['volr5_20']:.2f}",
                f"tsell={feat['tsell']:.3f}",
                f"drop5={feat['drop_5m'] * 100:.2f}%",
                f"tier={micro_decision['tier']}",
                f"agg_filter={micro_decision['reason']}",
                f"m0_40s_sell_ratio={micro_feat.get('m0_40s_sell_ratio', 0.0):.3f}",
                f"m0_50s_sell_ratio={micro_feat.get('m0_50s_sell_ratio', 0.0):.3f}",
                f"m0_50s_close_pos={micro_feat.get('m0_50s_close_pos', 0.5):.3f}",
                f"m0_59s_sell_ratio={micro_feat.get('m0_59s_sell_ratio', 0.0):.3f}",
                f"m0_59s_low_time_frac={micro_feat.get('m0_59s_low_time_frac', 0.0):.3f}",
                f"paper_margin={sizing['margin_usdt']:.2f}",
                f"paper_notional={sizing['notional_usdt']:.2f}",
                f"leverage={sizing['leverage']:.2f}",
            ]
            pos = WaterfallPosition(
                position_id=position_id,
                symbol=symbol,
                strategy=self.strategy,
                family=family,
                rule=rule.name,
                exit_profile=profile.name,
                status="open",
                side="SHORT",
                entry_time=now,
                entry_price=entry,
                notional_usdt=sizing["notional_usdt"],
                stop_price=stop,
                best_price=entry,
                worst_price=entry,
                trail_price=0.0,
                fee_rate=float(self.cfg["fee_rate"]),
                margin_usdt=sizing["margin_usdt"],
                leverage=sizing["leverage"],
                capital_fraction=sizing["capital_fraction"],
                evidence=evidence,
                updated_time=now,
            )
            sig = WaterfallSignal(
                signal_id=f"wf-open-{symbol}-{now}-{rule.name}",
                position_id=position_id,
                symbol=symbol,
                strategy=self.strategy,
                action="open_short",
                family=family,
                rule=rule.name,
                decision_time=now,
                price=entry,
                stop_price=stop,
                confidence=min(0.99, confidence_from_features(feat) + float(micro_decision["confidence_boost"])),
                tier=str(micro_decision["tier"]),
                notional_usdt=pos.notional_usdt,
                margin_usdt=pos.margin_usdt,
                leverage=pos.leverage,
                account_equity_usdt=sizing["equity_usdt"],
                evidence=evidence,
            )
            day = local_day_from_ms(now)
            self.trade_count_day[(symbol, day)] = self.trade_count_day.get((symbol, day), 0) + 1
            self.last_signal_time[symbol] = now
            self.last_family_time[(symbol, family)] = now
            return pos, sig
        return None

    def paper_sizing(self) -> dict[str, float]:
        equity = self.initial_balance_usdt + self.realized_pnl_usdt
        used_margin = sum(max(0.0, p.margin_usdt) for p in self.positions.values())
        free = max(0.0, equity - used_margin)
        fraction = max(0.0, min(1.0, self.margin_fraction))
        leverage = max(1.0, self.leverage)
        margin = min(free, max(0.0, equity * fraction))
        notional = margin * leverage
        fallback_notional = float(self.cfg.get("notional_usdt") or 0.0)
        if margin <= 0 and fallback_notional > 0 and free > 0:
            notional = min(fallback_notional, free * leverage)
            margin = notional / leverage
        return {
            "equity_usdt": equity,
            "free_usdt": free,
            "used_margin_usdt": used_margin,
            "margin_usdt": margin,
            "notional_usdt": notional,
            "leverage": leverage,
            "capital_fraction": fraction,
        }

    def micro_signal_decision(self, family: str, micro: dict[str, float]) -> dict[str, Any]:
        if not bool(self.cfg.get("require_agg_confirmation", True)):
            return {"ok": True, "tier": "normal", "reason": "agg_optional", "confidence_boost": 0.0}
        sell59 = float(micro.get("m0_59s_sell_ratio", 0.0))
        low59 = float(micro.get("m0_59s_low_time_frac", 0.0))
        sell50 = float(micro.get("m0_50s_sell_ratio", 0.0))
        close50 = float(micro.get("m0_50s_close_pos", 0.5))
        sell40 = float(micro.get("m0_40s_sell_ratio", 0.0))
        strong = (
            sell50 >= float(self.cfg["strong_agg_sell_ratio_min"])
            and close50 <= float(self.cfg["strong_agg_close_pos_max"])
        )
        if strong:
            return {"ok": True, "tier": "strong", "reason": "strong_sell_pressure_close_low", "confidence_boost": 0.12}
        if family == "downtrend_continuation":
            ok = sell40 >= float(self.cfg["downtrend_agg_sell_ratio_min"]) and low59 >= float(self.cfg["downtrend_low_time_frac_min"])
            return {"ok": ok, "tier": "normal", "reason": "downtrend_sell_pressure_late_low", "confidence_boost": 0.06 if ok else 0.0}
        if family == "momentum_dump":
            ok = low59 >= float(self.cfg["momentum_low_time_frac_min"]) and float(micro.get("m0_59s_close_pos", 0.5)) <= float(self.cfg["momentum_close_pos_max"])
            return {"ok": ok, "tier": "normal", "reason": "momentum_late_low_close_low", "confidence_boost": 0.04 if ok else 0.0}
        ok = sell59 >= float(self.cfg["agg_sell_ratio_min"]) and low59 >= float(self.cfg["agg_low_time_frac_min"])
        return {"ok": ok, "tier": "normal", "reason": "sell_pressure_late_low", "confidence_boost": 0.05 if ok else 0.0}

    def cooldown_ok(self, symbol: str, family: str, now: int) -> bool:
        day = local_day_from_ms(now)
        if self.trade_count_day.get((symbol, day), 0) >= int(self.cfg["max_trades_per_symbol_day"]):
            return False
        last = self.last_signal_time.get(symbol, 0)
        if last and now - last < float(self.cfg["same_symbol_cooldown_hours"]) * 3_600_000:
            return False
        stop = self.last_stop_time.get(symbol, 0)
        if stop and now - stop < float(self.cfg["after_stop_cooldown_hours"]) * 3_600_000:
            return False
        family_last = self.last_family_time.get((symbol, family), 0)
        if family_last and now - family_last < int(self.cfg["family_gap_minutes"]) * MINUTE_MS:
            return False
        return True

    def update_position(self, pos: WaterfallPosition, candle: Candle) -> WaterfallSignal | None:
        profile = self.profiles[pos.exit_profile]
        now = candle.close_time
        prev_best = pos.best_price
        prev_trail = pos.trail_price
        pos.worst_price = max(pos.worst_price, candle.high)
        pos.updated_time = now
        exit_price = 0.0
        reason = ""
        if candle.high >= pos.stop_price:
            exit_price = pos.stop_price
            reason = "stop_loss"
        else:
            mfe = pos.entry_price / prev_best - 1.0 if prev_best > 0 else 0.0
            if prev_trail > 0 and candle.high >= prev_trail:
                exit_price = prev_trail
                reason = "take_profit_trailing"
            age_min = max(0, int((now - pos.entry_time) / MINUTE_MS))
            if not reason and age_min <= 3:
                broken_level = evidence_float(pos.evidence, "broken_level", 0.0)
                if broken_level and candle.close > broken_level * (1.0 + profile.quick_reclaim_buffer):
                    exit_price = candle.close
                    reason = "stop_quick_reclaim"
            if not reason and mfe >= profile.rebound_activate:
                rebound = candle.close / prev_best - 1.0 if prev_best > 0 else 0.0
                if rebound >= profile.rebound_retrace:
                    exit_price = candle.close
                    reason = "take_profit_rebound"
            if not reason and age_min >= profile.max_hold_min:
                exit_price = candle.close
                reason = "timeout_exit"
        if not reason:
            pos.best_price = min(pos.best_price, candle.low)
            mfe = pos.entry_price / pos.best_price - 1.0 if pos.best_price > 0 else 0.0
            if pos.trail_price <= 0 and mfe >= profile.trail_activate:
                pos.trail_price = pos.best_price * (1.0 + profile.trail_rebound)
            elif pos.trail_price > 0:
                pos.trail_price = min(pos.trail_price, pos.best_price * (1.0 + profile.trail_rebound))
            return None
        pos.status = "closed"
        pos.exit_time = now
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.pnl_pct = 1.0 - exit_price / pos.entry_price - pos.fee_rate if exit_price > 0 and pos.entry_price > 0 else 0.0
        pos.pnl_usdt = pos.notional_usdt * pos.pnl_pct
        self.realized_pnl_usdt += pos.pnl_usdt
        if reason.startswith("stop"):
            self.last_stop_time[pos.symbol] = now
        action = "take_profit" if reason.startswith("take_profit") or pos.pnl_pct > 0 else "stop_loss"
        if reason == "timeout_exit":
            action = "timeout_exit"
        return WaterfallSignal(
            signal_id=f"wf-exit-{pos.symbol}-{now}-{reason}",
            position_id=pos.position_id,
            symbol=pos.symbol,
            strategy=pos.strategy,
            action=action,
            family=pos.family,
            rule=pos.rule,
            decision_time=now,
            price=exit_price,
            stop_price=pos.stop_price,
            pnl_pct=pos.pnl_pct,
            confidence=0.0,
            tier="exit",
            notional_usdt=pos.notional_usdt,
            margin_usdt=pos.margin_usdt,
            leverage=pos.leverage,
            account_equity_usdt=self.initial_balance_usdt + self.realized_pnl_usdt,
            evidence=[*pos.evidence, f"exit_reason={reason}", f"mfe={(pos.entry_price / pos.best_price - 1.0) * 100:.2f}%"],
        )


async def waterfall_monitor(
    settings: dict[str, Any],
    broad_top: int | None = None,
    discover_every: str | None = None,
    samples: int = 0,
    max_workers: int | None = None,
) -> None:
    dirs = ensure_dirs(settings)
    store = Store(dirs["db"])
    client = BinanceRestClient(settings)
    sink = WaterfallSignalSink(dirs["alerts"], settings)
    executor = WaterfallExecutionAdapter(settings)
    engine = WaterfallEngine(settings)
    engine.load_positions(store.active_waterfall_positions())
    engine.load_recent_state(store.waterfall_position_rows(limit=1000), store.waterfall_signal_rows(limit=1000))
    extra_engines: list[Any] = []
    from .board_waterfall import BoardWaterfallEngine, board_waterfall_settings

    if bool(board_waterfall_settings(settings).get("enabled", True)):
        board_engine = BoardWaterfallEngine(settings)
        board_engine.load_positions(store.active_waterfall_positions())
        board_engine.load_recent_state(store.waterfall_position_rows(limit=1000), store.waterfall_signal_rows(limit=1000))
        extra_engines.append(board_engine)
    cfg = waterfall_settings(settings)
    top = int(broad_top or cfg["broad_top"])
    interval = str(cfg["watch_interval"])
    if interval != "1m":
        raise ValueError("waterfall_quant currently requires watch_interval=1m")
    every = parse_duration_seconds(discover_every or str(cfg["discover_every"]))
    workers = int(max_workers or cfg["max_workers"])
    processed = 0
    while True:
        try:
            symbols = refresh_waterfall_universe(client, store, engine, settings, top, workers, extra_engines=extra_engines)
            break
        except Exception as exc:
            # Rate-limit bans (HTTP 418/429) must wait, not crash: a systemd
            # restart loop re-prewarms the full universe and extends the ban.
            print(f"[{local_stamp()}] waterfall universe error: {exc}; retry in 180s", flush=True)
            await asyncio.sleep(180)
    micro_streams = [str(x) for x in cfg.get("micro_streams", ["aggTrade"]) if str(x)]
    source = WebSocketMarketSource(settings, symbols, ["1m"], micro_streams=micro_streams)
    agen = source.market_events()
    next_discovery = utc_ms() + every * 1000
    print(f"waterfall strategy={engine.strategy} symbols={len(symbols)}", flush=True)
    try:
        while samples <= 0 or processed < samples:
            timeout = max(1.0, (next_discovery - utc_ms()) / 1000.0)
            try:
                event = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
            except asyncio.TimeoutError:
                await source.close()
                await agen.aclose()
                try:
                    symbols = refresh_waterfall_universe(client, store, engine, settings, top, workers, extra_engines=extra_engines)
                except Exception as exc:
                    print(f"[{local_stamp()}] waterfall re-discovery error: {exc}; keep old universe", flush=True)
                source = WebSocketMarketSource(settings, symbols, ["1m"], micro_streams=micro_streams)
                agen = source.market_events()
                next_discovery = utc_ms() + every * 1000
                print(f"waterfall websocket symbols={len(symbols)} streams={len(source.stream_names())}", flush=True)
                continue
            processed += 1
            if isinstance(event, dict):
                engine.on_micro(event)
                if bool(cfg.get("store_micro_events", False)):
                    store.save_waterfall_shadow_events([event])
                if processed % int(settings.get("websocket", {}).get("heartbeat_events", 250)) == 0:
                    print(f"[{local_stamp()}] waterfall events={processed} last={event.get('symbol')} open_positions={len(engine.positions)}", flush=True)
                continue
            store.save_candles([event.candle])
            watch, positions, signals = engine.on_kline(event)
            for eng in extra_engines:
                _w2, p2, s2 = eng.on_kline(event)
                positions = [*positions, *p2]
                signals = [*signals, *s2]
            store.upsert_waterfall_watch(watch)
            for pos in positions:
                store.upsert_waterfall_position(pos.to_dict())
            for signal in signals:
                executor.handle_signal(signal)
                pushed, msg = sink.emit(signal)
                store.save_waterfall_signal(signal.to_dict(), pushed=pushed, push_error="" if pushed else msg)
            if processed % int(settings.get("websocket", {}).get("heartbeat_events", 250)) == 0:
                print(f"[{local_stamp()}] waterfall events={processed} last={event.symbol} open_positions={len(engine.positions)}", flush=True)
    finally:
        await source.close()
        await agen.aclose()
        print(f"[{local_stamp()}] waterfall monitor stopped events={processed}", flush=True)


async def waterfall_shadow_collect(
    settings: dict[str, Any],
    symbols: list[str] | None = None,
    broad_top: int | None = None,
    seconds: int = 600,
    max_events: int = 0,
) -> None:
    dirs = ensure_dirs(settings)
    store = Store(dirs["db"])
    client = BinanceRestClient(settings)
    cfg = waterfall_settings(settings)
    if symbols:
        watch_symbols = sorted({s.upper() for s in symbols if s.strip()})
    else:
        top = int(broad_top or cfg["broad_top"])
        watch_symbols = [str(r["symbol"]) for r in build_broad_universe(client, settings, broad_top=top)]
    streams = micro_stream_names(watch_symbols)
    base_url = settings["network"]["ws_base_url"].rstrip("/")
    url = f"{base_url}/stream?streams={'/'.join(streams)}"
    print(f"[{local_stamp()}] waterfall shadow symbols={len(watch_symbols)} streams={len(streams)} seconds={seconds}", flush=True)
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets is required: pip install -r requirements.txt") from exc

    deadline = utc_ms() + int(seconds) * 1000
    saved = 0
    batch: list[dict[str, Any]] = []
    async with websockets.connect(url, ping_interval=120, ping_timeout=20, close_timeout=5) as ws:
        while utc_ms() < deadline and (max_events <= 0 or saved < max_events):
            timeout = max(0.5, (deadline - utc_ms()) / 1000.0)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            row = parse_micro_message(raw)
            if row:
                batch.append(row)
            if len(batch) >= 500:
                saved += store.save_waterfall_shadow_events(batch)
                batch.clear()
                print(f"[{local_stamp()}] waterfall shadow saved={saved}", flush=True)
        if batch:
            saved += store.save_waterfall_shadow_events(batch)
    print(f"[{local_stamp()}] waterfall shadow complete saved={saved}", flush=True)


def micro_stream_names(symbols: list[str]) -> list[str]:
    out: list[str] = []
    for symbol in sorted({s.upper() for s in symbols}):
        lower = symbol.lower()
        out.append(f"{lower}@aggTrade")
        out.append(f"{lower}@bookTicker")
    return out


def parse_micro_message(raw: str | bytes) -> dict[str, Any] | None:
    payload = json.loads(raw)
    stream = str(payload.get("stream") or "")
    data = payload.get("data", payload)
    symbol = str(data.get("s") or "").upper()
    if not symbol:
        return None
    event_time = int(data.get("E") or data.get("T") or utc_ms())
    if "aggTrade" in stream or data.get("e") == "aggTrade":
        kind = "aggTrade"
    elif "bookTicker" in stream or data.get("e") == "bookTicker":
        kind = "bookTicker"
    else:
        kind = stream.rsplit("@", 1)[-1] if "@" in stream else str(data.get("e") or "unknown")
    return {
        "symbol": symbol,
        "event_time": event_time,
        "stream": kind,
        "payload": data,
        "created_time": utc_ms(),
    }


def refresh_waterfall_universe(
    client: BinanceRestClient,
    store: Store,
    engine: WaterfallEngine,
    settings: dict[str, Any],
    broad_top: int,
    max_workers: int,
    extra_engines: list[Any] | None = None,
) -> list[str]:
    rows = build_broad_universe(client, settings, broad_top=broad_top)
    symbols = [str(r["symbol"]) for r in rows]
    active = [str(r["symbol"]) for r in store.active_waterfall_positions()]
    symbols = sorted(set(symbols) | set(active))
    print(f"[{local_stamp()}] waterfall universe symbols={len(symbols)} broad_top={broad_top}", flush=True)
    prewarm_waterfall_symbols(client, store, engine, symbols, int(engine.cfg["prewarm_limit"]), max_workers, extra_engines=extra_engines)
    return symbols


def prewarm_waterfall_symbols(
    client: BinanceRestClient,
    store: Store,
    engine: WaterfallEngine,
    symbols: list[str],
    limit: int,
    max_workers: int,
    extra_engines: list[Any] | None = None,
) -> None:
    cutoff = closed_candle_cutoff_ms(utc_ms(), "1m")
    changed: list[dict[str, Any]] = []
    total = 0
    errors: list[str] = []

    def fetch(symbol: str) -> tuple[str, list[Candle]]:
        candles = [c for c in client.klines(symbol, "1m", limit=limit) if c.close_time <= cutoff]
        return symbol, candles

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch, s): s for s in symbols}
        for fut in as_completed(futs):
            symbol = futs[fut]
            try:
                _sym, candles = fut.result()
                total += len(candles)
                store.save_candles(candles)
                changed.extend(engine.prime_candles(candles))
                for eng in extra_engines or []:
                    eng.prime_candles(candles)
            except Exception as exc:
                errors.append(f"{symbol}={type(exc).__name__}: {exc}"[:160])
    store.upsert_waterfall_watch(changed)
    print(f"[{local_stamp()}] waterfall prewarm candles={total} watch={len(changed)} errors={len(errors)}", flush=True)
    if errors:
        print(f"[{local_stamp()}] waterfall prewarm error preview: {'; '.join(errors[:5])}", flush=True)


class WaterfallSignalSink:
    def __init__(self, alerts_dir: str | Path, settings: dict[str, Any]):
        self.alerts_dir = Path(alerts_dir)
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.webhook_url = (settings.get("notify") or {}).get("wecom_webhook_url") or os.environ.get("WECOM_WEBHOOK_URL", "")
        self.push_wecom = bool(waterfall_settings(settings).get("push_wecom", True))

    def emit(self, signal: WaterfallSignal) -> tuple[bool, str]:
        print(render_waterfall_console(signal), flush=True)
        self.write_files(signal)
        if self.webhook_url and self.push_wecom:
            return self.push(signal)
        return False, ""

    def write_files(self, signal: WaterfallSignal) -> None:
        day = local_day_from_ms(signal.decision_time)
        jsonl = self.alerts_dir / f"waterfall-{day}.jsonl"
        md = self.alerts_dir / f"waterfall-{day}.md"
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(signal.to_dict(), ensure_ascii=False) + "\n")
        with md.open("a", encoding="utf-8") as fh:
            fh.write(render_waterfall_markdown(signal) + "\n\n")

    def push(self, signal: WaterfallSignal) -> tuple[bool, str]:
        payload = {"msgtype": "markdown", "markdown": {"content": render_waterfall_wecom(signal)}}
        req = Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            if data.get("errcode") not in (0, "0"):
                return False, f"wecom errcode={data.get('errcode')} errmsg={data.get('errmsg', body)}"[:200]
            return True, body
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]


class WaterfallExecutionAdapter:
    """Paper-first execution adapter for the waterfall quant strategy.

    Real order placement is intentionally not implemented here yet. The class is
    wired into the signal flow so a future Binance order adapter has one small
    integration point, while the current server remains paper-only.
    """

    def __init__(self, settings: dict[str, Any]):
        cfg = waterfall_settings(settings)
        self.mode = str(cfg.get("execution_mode", "paper")).lower()
        self.real_order_enabled = bool(cfg.get("real_order_enabled", False))
        if self.mode not in {"paper", "live"}:
            raise ValueError(f"unknown waterfall execution_mode={self.mode!r}")
        if self.mode == "live":
            if not self.real_order_enabled:
                raise RuntimeError("waterfall live mode requires real_order_enabled=true")
            raise NotImplementedError("waterfall real Binance order adapter is reserved but not implemented")

    def handle_signal(self, signal: WaterfallSignal) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "real_order_enabled": self.real_order_enabled,
            "sent": False,
            "action": signal.action,
            "symbol": signal.symbol,
        }


def render_waterfall_console(signal: WaterfallSignal) -> str:
    return (
        f"[{iso_from_ms(signal.decision_time)}] waterfall {signal.action} {signal.symbol} "
        f"price={signal.price:.8g} stop={signal.stop_price:.8g} pnl={signal.pnl_pct * 100:.2f}% "
        f"family={signal.family} tier={signal.tier} margin={signal.margin_usdt:.2f} "
        f"notional={signal.notional_usdt:.2f} rule={signal.rule}"
    )


def render_waterfall_markdown(signal: WaterfallSignal) -> str:
    return "\n".join(
        [
            f"### WATERFALL {signal.action} {signal.symbol} {iso_from_ms(signal.decision_time)}",
            f"- price: {signal.price:.12g}",
            f"- stop: {signal.stop_price:.12g}",
            f"- pnl: {signal.pnl_pct * 100:.2f}%",
            f"- family: {signal.family}",
            f"- rule: {signal.rule}",
            f"- confidence: {signal.confidence:.3f}",
            f"- tier: {signal.tier}",
            f"- margin_usdt: {signal.margin_usdt:.4f}",
            f"- notional_usdt: {signal.notional_usdt:.4f}",
            f"- leverage: {signal.leverage:.2f}x",
            f"- account_equity_usdt: {signal.account_equity_usdt:.4f}",
            f"- evidence: {'; '.join(signal.evidence)}",
        ]
    )


STRATEGY_LABELS = {
    "claude_board_wf_1m": "Claude·冠军标签",
}


def strategy_label(strategy: str) -> str:
    return STRATEGY_LABELS.get(strategy, "Codex·core5_agg")


def render_waterfall_wecom(signal: WaterfallSignal) -> str:
    action_cn = {
        "open_short": "瀑布开空",
        "take_profit": "瀑布止盈",
        "stop_loss": "瀑布止损",
        "timeout_exit": "瀑布超时离场",
    }.get(signal.action, signal.action)
    lines = [
        f"**[{strategy_label(signal.strategy)}] {action_cn} {signal.symbol}**",
        f"> 价格 {signal.price:.8g} | 止损 {signal.stop_price:.8g}",
        f"> 档位 {signal.tier} | 置信 {signal.confidence:.3f}",
        f"> 类型 {signal.family} | 规则 {signal.rule}",
    ]
    if signal.action != "open_short":
        lines.append(f"> 收益 {signal.pnl_pct * 100:.2f}% | 权益 {signal.account_equity_usdt:.2f}U")
    else:
        lines.append(f"> 保证金 {signal.margin_usdt:.2f}U | 名义 {signal.notional_usdt:.2f}U | {signal.leverage:.1f}x")
    return "\n".join(lines)


def signal_ok(feat: dict[str, float], rule: WaterfallRule) -> bool:
    if feat["qv30"] < rule.min_qv30:
        return False
    if feat["family_code"] != family_code(rule.family):
        return False
    if not (rule.min_ret_30m <= feat["ret_30m"] <= rule.max_ret_30m):
        return False
    if not (rule.min_ret_2h <= feat["ret_2h"] <= rule.max_ret_2h):
        return False
    if not (rule.min_ret_4h <= feat["ret_4h"] <= rule.max_ret_4h):
        return False
    if not (rule.min_ret_12h <= feat["ret_12h"] <= rule.max_ret_12h):
        return False
    if not (rule.min_ret_24h <= feat["ret_24h"] <= rule.max_ret_24h):
        return False
    if feat["drop_5m"] < rule.min_drop_5m_entry:
        return False
    if not (rule.min_runup_24h <= feat["runup_24h"] <= rule.max_runup_24h):
        return False
    if not (rule.min_dd_from_24h_high <= feat["dd_from_24h_high"] <= rule.max_dd_from_24h_high):
        return False
    if not (rule.min_qv_over_prev6max <= feat["qv_over_prev6max"] <= rule.max_qv_over_prev6max):
        return False
    if int(feat["red_streak"]) < rule.min_red_streak:
        return False
    if feat["close_pos"] > rule.max_close_pos:
        return False
    if feat["tsell"] < rule.min_tsell:
        return False
    if feat["upper_wick"] < rule.min_upper_wick:
        return False
    if not (rule.min_lower_wick <= feat["lower_wick"] <= rule.max_lower_wick):
        return False
    if not (rule.min_range_pct <= feat["range_pct"] <= rule.max_range_pct):
        return False
    if feat["volr20"] > rule.max_volr20 or feat["volr5_20"] > rule.max_volr5_20:
        return False
    prior_low = feat.get(f"prior_body_low_{rule.break_lookback}", 0.0)
    if prior_low <= 0 or feat["close"] >= prior_low * (1.0 - rule.break_buffer):
        return False
    one_bar = feat["body_drop"] >= rule.min_body_drop and feat["volr20"] >= rule.min_volr20
    two_bar = feat["drop_2m"] >= rule.min_2m_drop and feat["volr20"] >= rule.min_volr20
    five_bar = feat["drop_5m"] >= rule.min_5m_drop and feat["volr5_20"] >= rule.min_volr5_20
    return bool(one_bar or two_bar or five_bar)


def classify_family(feat: dict[str, float]) -> str:
    if feat.get("ret_24h", 0.0) >= 0.28 or feat.get("runup_24h", 0.0) >= 0.45:
        return "post_pump"
    if feat.get("ret_4h", 0.0) <= -0.08 and feat.get("ret_30m", 0.0) <= -0.015:
        return "downtrend_continuation"
    if abs(feat.get("ret_4h", 0.0)) <= 0.06 and feat.get("dd_from_24h_high", 0.0) <= 0.18:
        return "range_breakdown"
    if feat.get("ret_30m", 0.0) <= -0.04:
        return "momentum_dump"
    return "other"


def family_code(name: str) -> float:
    return {
        "post_pump": 1.0,
        "downtrend_continuation": 2.0,
        "range_breakdown": 3.0,
        "momentum_dump": 4.0,
        "other": 5.0,
    }.get(name, 0.0)


def confidence_from_features(feat: dict[str, float]) -> float:
    score = 0.45
    score += min(0.18, max(0.0, (feat["volr20"] - 2.0) * 0.035))
    score += min(0.15, max(0.0, (feat["tsell"] - 0.52) * 0.7))
    score += min(0.12, max(0.0, feat["drop_5m"] * 2.0))
    score += min(0.10, max(0.0, feat["dd_from_24h_high"] * 0.5))
    return min(0.99, score)


def red_streak(candles: list[Candle]) -> int:
    streak = 0
    for candle in reversed(candles[-20:]):
        if candle.close < candle.open:
            streak += 1
        else:
            break
    return streak


def evidence_float(evidence: list[str], key: str, default: float) -> float:
    prefix = f"{key}="
    for item in evidence:
        if item.startswith(prefix):
            try:
                return float(item.split("=", 1)[1])
            except ValueError:
                return default
    return default


def safe_div(num: float, den: float, default: float) -> float:
    try:
        if den == 0:
            return default
        out = num / den
        return out if out == out and abs(out) != float("inf") else default
    except Exception:
        return default


def waterfall_position_from_row(row: dict[str, Any]) -> WaterfallPosition:
    evidence = row.get("evidence")
    if evidence is None:
        raw = row.get("evidence_json", "[]")
        try:
            evidence = json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            evidence = []
    return WaterfallPosition(
        position_id=str(row["position_id"]),
        symbol=str(row["symbol"]),
        strategy=str(row["strategy"]),
        family=str(row["family"]),
        rule=str(row["rule"]),
        exit_profile=str(row["exit_profile"]),
        status=str(row["status"]),
        side=str(row["side"]),
        entry_time=int(row["entry_time"]),
        entry_price=float(row["entry_price"]),
        notional_usdt=float(row["notional_usdt"]),
        stop_price=float(row["stop_price"]),
        best_price=float(row["best_price"]),
        worst_price=float(row["worst_price"]),
        trail_price=float(row.get("trail_price") or 0.0),
        exit_time=row.get("exit_time"),
        exit_price=float(row.get("exit_price") or 0.0),
        pnl_pct=float(row.get("pnl_pct") or 0.0),
        pnl_usdt=float(row.get("pnl_usdt") or 0.0),
        exit_reason=str(row.get("exit_reason") or ""),
        fee_rate=float(row.get("fee_rate") or DEFAULT_FEE_RATE),
        margin_usdt=float(row.get("margin_usdt") or 0.0),
        leverage=float(row.get("leverage") or 1.0),
        capital_fraction=float(row.get("capital_fraction") or 0.0),
        evidence=list(evidence or []),
        updated_time=int(row.get("updated_time") or row.get("entry_time") or 0),
    )
