"""Claude board-waterfall strategy engine (champion label).

Independent paper strategy running alongside the codex core5_agg engine on
the same 1m candle stream and infrastructure, with its own paper account.

Label (validated 2023-2025 selection / 2026H1 verdict, walk-forward clean):
  * board coin: 24h return >= +40%
  * detection: close <= 60m rolling high * (1 - 7%)
  * liquidity:  60m quote volume >= 300k USDT
Entry: on the confirming 1m close (tick-early entry was rejected: 62% of
intra-minute breaks are wicks).
Exit (E1, winner of an 8-variant battle):
  * structure stop at B*1.01 where B = highest price after the 60m low
    (min entry*1.015)
  * trailing profit: activate at MFE>=3.5%, rebound 3.0%, prev-bar-confirmed
  * time stop 240 minutes
Cooldown 6h per symbol (relay re-entry rejected: bounce follows our exits).

Verdict-period stats (2026H1, 0.30% round-trip cost): 3.28 trades/day,
win 67%, +0.40%/trade, PF 1.21; with far-depth gate +0.64%/PF1.36 (gate
needs depth polling, configurable later).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from .models import Candle, KlineClosed
from .timeutils import local_day_from_ms
from .waterfall import (
    DEFAULT_FEE_RATE,
    WaterfallPosition,
    WaterfallSignal,
)

MINUTE_MS = 60_000
STRATEGY_NAME = "claude_board_wf_1m"


def board_waterfall_settings(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(settings.get("claude_board_waterfall") or {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("paper_initial_balance_usdt", 100.0)
    cfg.setdefault("paper_margin_fraction", 0.2)
    cfg.setdefault("leverage", 10.0)
    cfg.setdefault("max_open_positions", 5)
    cfg.setdefault("fee_rate", DEFAULT_FEE_RATE)
    cfg.setdefault("slippage_bps", 10)  # one-sided market-order slippage (latency folded in); ~0.20% round trip
    cfg.setdefault("min_ret_24h", 0.40)
    cfg.setdefault("break_window_min", 60)
    cfg.setdefault("break_drop", 0.07)
    cfg.setdefault("min_qv60_usdt", 300_000.0)
    cfg.setdefault("stop_bounce_buffer", 0.01)
    cfg.setdefault("stop_min_pct", 0.015)
    cfg.setdefault("trail_activate", 0.035)
    cfg.setdefault("trail_rebound", 0.030)
    cfg.setdefault("max_hold_min", 240)
    cfg.setdefault("same_symbol_cooldown_hours", 6.0)
    cfg.setdefault("max_trades_per_symbol_day", 2)
    return cfg


class BoardWaterfallEngine:
    """Champion-label short engine; same interface as WaterfallEngine."""

    def __init__(self, settings: dict[str, Any], shared_candles: dict[str, deque[Candle]] | None = None):
        self.settings = settings
        self.cfg = board_waterfall_settings(settings)
        self.strategy = STRATEGY_NAME
        self.maxlen = 1500
        # Share the codex engine's candle deques instead of keeping a second
        # full copy — two independent 1500-candle-per-symbol stores doubled the
        # candle memory and OOM'd the 2G box. When shared, this engine never
        # appends (the codex engine populates the dict before we read it).
        self._shared = shared_candles is not None
        self.candles: dict[str, deque[Candle]] = shared_candles if shared_candles is not None else {}
        self.positions: dict[str, WaterfallPosition] = {}
        self.last_signal_time: dict[str, int] = {}
        self.trade_count_day: dict[tuple[str, str], int] = {}
        self.realized_pnl_usdt = 0.0
        self.initial_balance_usdt = float(self.cfg["paper_initial_balance_usdt"])
        self.leverage = float(self.cfg["leverage"])
        self.margin_fraction = float(self.cfg["paper_margin_fraction"])

    # -- state loading (same shape as WaterfallEngine) ------------------

    def load_positions(self, rows: list[dict[str, Any]]) -> None:
        from .waterfall import waterfall_position_from_row

        for row in rows:
            if str(row.get("strategy") or "") != self.strategy:
                continue
            pos = waterfall_position_from_row(row)
            if pos.status == "open":
                self.positions[pos.symbol] = pos

    def load_recent_state(self, positions: list[dict[str, Any]], signals: list[dict[str, Any]]) -> None:
        for row in positions:
            if str(row.get("strategy") or "") != self.strategy:
                continue
            symbol = str(row.get("symbol") or "")
            entry_time = int(row.get("entry_time") or 0)
            if symbol and entry_time:
                self.last_signal_time[symbol] = max(self.last_signal_time.get(symbol, 0), entry_time)
                day = local_day_from_ms(entry_time)
                self.trade_count_day[(symbol, day)] = self.trade_count_day.get((symbol, day), 0) + 1
            if str(row.get("status") or "") == "closed":
                self.realized_pnl_usdt += float(row.get("pnl_usdt") or 0.0)

    def on_micro(self, row: dict[str, Any]) -> None:
        return  # label is pure 1m kline; micro tiers may be added after paper A/B

    # -- candle flow -----------------------------------------------------

    def prime_candles(self, candles: list[Candle]) -> list[dict[str, Any]]:
        if self._shared:
            return []  # codex engine already primed the shared dict
        for candle in sorted(candles, key=lambda c: (c.symbol, c.open_time)):
            self._append(candle)
        return []

    def on_kline(self, event: KlineClosed) -> tuple[list[dict[str, Any]], list[WaterfallPosition], list[WaterfallSignal]]:
        if event.interval != "1m":
            return [], [], []
        candle = event.candle
        if not self._shared:
            self._append(candle)  # shared: codex engine appended before us
        changed_positions: list[WaterfallPosition] = []
        signals: list[WaterfallSignal] = []

        pos = self.positions.get(candle.symbol)
        if pos:
            exit_signal = self.update_position(pos, candle)
            changed_positions.append(pos)
            if exit_signal:
                signals.append(exit_signal)
                self.positions.pop(candle.symbol, None)

        if candle.symbol not in self.positions:
            entry = self.entry_signal(candle.symbol, candle)
            if entry:
                position, signal = entry
                self.positions[candle.symbol] = position
                changed_positions.append(position)
                signals.append(signal)
        return [], changed_positions, signals

    def _append(self, candle: Candle) -> None:
        dq = self.candles.setdefault(candle.symbol, deque(maxlen=self.maxlen))
        if dq and dq[-1].open_time == candle.open_time:
            dq[-1] = candle
        elif not dq or candle.open_time > dq[-1].open_time:
            dq.append(candle)

    # -- label -----------------------------------------------------------

    def entry_signal(self, symbol: str, candle: Candle) -> tuple[WaterfallPosition, WaterfallSignal] | None:
        values = list(self.candles.get(symbol, []))
        if len(values) < 1441:
            return None
        if len(self.positions) >= int(self.cfg["max_open_positions"]):
            return None
        now = candle.close_time
        if not self._cooldown_ok(symbol, now):
            return None
        close = candle.close
        if close <= 0:
            return None
        ref = values[-1441].close
        if ref <= 0 or close / ref - 1.0 < float(self.cfg["min_ret_24h"]):
            return None
        window = values[-int(self.cfg["break_window_min"]):]
        hi = max(x.high for x in window)
        if close > hi * (1.0 - float(self.cfg["break_drop"])):
            return None
        qv60 = sum(x.quote_volume for x in window)
        if qv60 < float(self.cfg["min_qv60_usdt"]):
            return None
        low_i = min(range(len(window)), key=lambda k: window[k].low)
        bounce_high = max(x.high for x in window[low_i:])
        # Realistic entry: a market SELL-to-open fills below the signal close
        # (crossing the spread + adverse drift during the ~sub-second latency of
        # a fast dump). slippage_bps is one-sided; latency is folded in.
        slip = float(self.cfg.get("slippage_bps", 10)) / 10000.0
        entry = close * (1.0 - slip)
        stop = max(bounce_high * (1.0 + float(self.cfg["stop_bounce_buffer"])), entry * (1.0 + float(self.cfg["stop_min_pct"])))
        sizing = self.paper_sizing()
        position_id = f"cbwf-{symbol}-{now}"
        evidence = [
            f"strategy={self.strategy}",
            f"ret24={(close / ref - 1.0) * 100:.1f}%",
            f"break60m={(close / hi - 1.0) * 100:.1f}%",
            f"qv60={qv60:.0f}",
            f"bounce_high={bounce_high:.10g}",
            f"paper_margin={sizing['margin_usdt']:.2f}",
            f"paper_notional={sizing['notional_usdt']:.2f}",
        ]
        pos = WaterfallPosition(
            position_id=position_id,
            symbol=symbol,
            strategy=self.strategy,
            family="board_waterfall",
            rule="board40_drop7_60m",
            exit_profile="claude_e1",
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
            signal_id=f"cbwf-open-{symbol}-{now}",
            position_id=position_id,
            symbol=symbol,
            strategy=self.strategy,
            action="open_short",
            family="board_waterfall",
            rule="board40_drop7_60m",
            decision_time=now,
            price=entry,
            stop_price=stop,
            confidence=0.67,
            tier="normal",
            notional_usdt=sizing["notional_usdt"],
            margin_usdt=sizing["margin_usdt"],
            leverage=sizing["leverage"],
            account_equity_usdt=sizing["equity_usdt"],
            evidence=evidence,
        )
        day = local_day_from_ms(now)
        self.trade_count_day[(symbol, day)] = self.trade_count_day.get((symbol, day), 0) + 1
        self.last_signal_time[symbol] = now
        return pos, sig

    def _cooldown_ok(self, symbol: str, now: int) -> bool:
        day = local_day_from_ms(now)
        if self.trade_count_day.get((symbol, day), 0) >= int(self.cfg["max_trades_per_symbol_day"]):
            return False
        last = self.last_signal_time.get(symbol, 0)
        if last and now - last < float(self.cfg["same_symbol_cooldown_hours"]) * 3_600_000:
            return False
        return True

    def paper_sizing(self) -> dict[str, float]:
        equity = max(0.0, self.initial_balance_usdt + self.realized_pnl_usdt)
        margin = equity * self.margin_fraction
        notional = margin * self.leverage
        return {
            "equity_usdt": equity,
            "margin_usdt": margin,
            "notional_usdt": notional,
            "leverage": self.leverage,
            "capital_fraction": self.margin_fraction,
        }

    # -- exit (E1, prev-bar-confirmed trail; no lookahead) ----------------

    def update_position(self, pos: WaterfallPosition, candle: Candle) -> WaterfallSignal | None:
        now = candle.close_time
        prev_trail = pos.trail_price
        prev_best = pos.best_price
        pos.worst_price = max(pos.worst_price, candle.high)
        pos.updated_time = now
        exit_price = 0.0
        reason = ""
        if candle.high >= pos.stop_price:
            exit_price = pos.stop_price
            reason = "stop_loss"
        elif prev_trail > 0 and candle.high >= prev_trail:
            exit_price = prev_trail
            reason = "take_profit_trailing"
        else:
            age_min = max(0, int((now - pos.entry_time) / MINUTE_MS))
            if age_min >= int(self.cfg["max_hold_min"]):
                exit_price = candle.close
                reason = "timeout_exit"
        if not reason:
            pos.best_price = min(pos.best_price, candle.low)
            mfe = pos.entry_price / pos.best_price - 1.0 if pos.best_price > 0 else 0.0
            if pos.trail_price <= 0 and mfe >= float(self.cfg["trail_activate"]):
                pos.trail_price = pos.best_price * (1.0 + float(self.cfg["trail_rebound"]))
            elif pos.trail_price > 0:
                pos.trail_price = min(pos.trail_price, pos.best_price * (1.0 + float(self.cfg["trail_rebound"])))
            return None
        # Realistic exit: a market BUY-to-cover fills above the trigger level
        # (crossing the spread; stops during a fast move are worse still). Same
        # one-sided slippage_bps, adverse to the short.
        slip = float(self.cfg.get("slippage_bps", 10)) / 10000.0
        fill = exit_price * (1.0 + slip)
        pos.status = "closed"
        pos.exit_time = now
        pos.exit_price = fill
        pos.exit_reason = reason
        pos.pnl_pct = 1.0 - fill / pos.entry_price - pos.fee_rate if fill > 0 and pos.entry_price > 0 else 0.0
        pos.pnl_usdt = pos.notional_usdt * pos.pnl_pct
        pos.best_price = min(prev_best, candle.low)
        self.realized_pnl_usdt += pos.pnl_usdt
        action = "take_profit" if pos.pnl_pct > 0 else "stop_loss"
        if reason == "timeout_exit":
            action = "timeout_exit"
        return WaterfallSignal(
            signal_id=f"cbwf-exit-{pos.symbol}-{now}-{reason}",
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
            notional_usdt=pos.notional_usdt,
            margin_usdt=pos.margin_usdt,
            leverage=pos.leverage,
            account_equity_usdt=max(0.0, self.initial_balance_usdt + self.realized_pnl_usdt),
            evidence=[*pos.evidence, f"exit_reason={reason}"],
        )
