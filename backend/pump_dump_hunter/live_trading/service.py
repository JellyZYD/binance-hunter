from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..board_waterfall import BoardWaterfallEngine, STRATEGY_NAME
from ..config import ensure_dirs, resolve_path
from ..data.rest_client import BinanceRestClient
from ..data.store import Store
from ..data.websocket_source import WebSocketMarketSource
from ..models import Candle, KlineClosed
from ..sysmon import read_monitor_health
from ..timeutils import parse_duration_seconds, utc_ms
from ..waterfall import WaterfallSignal, refresh_waterfall_universe
from .config import LiveTradingConfig
from .credentials import BinanceCredentials
from .exchange_rules import ExchangeRules
from .gateway import BinanceGateway
from .ledger import LiveLedger
from .models import IntentAction, OrderState, TradeIntent
from .notifier import LiveEventNotifier
from .oms import LiveOrderManager, quote_from_api
from .risk import account_snapshot_from_api
from .signal_source import SharedPaperSignalSource, SignalCursor


D = Decimal
MINUTE_MS = 60_000


def missing_entry_history_opens(
    candles: list[Candle],
    decision_open_time: int,
    *,
    lookback_minutes: int = 1440,
) -> list[int]:
    """Return missing closed 1m opens required by a board-strategy entry.

    The current candle is deliberately excluded: callers repair only the
    historical window, then let the normal live event process the current
    candle once. This prevents a reconnect from creating a retroactive order.
    """
    start = int(decision_open_time) - int(lookback_minutes) * MINUTE_MS
    present = {
        int(candle.open_time)
        for candle in candles
        if start <= int(candle.open_time) < int(decision_open_time)
    }
    return [
        open_time
        for open_time in range(start, int(decision_open_time), MINUTE_MS)
        if open_time not in present
    ]


def universe_requires_stream_rebuild(current: list[str], refreshed: list[str]) -> bool:
    """Only reconnect public market streams when the subscribed set changed."""
    return {str(symbol).upper() for symbol in current} != {
        str(symbol).upper() for symbol in refreshed
    }


@dataclass(frozen=True)
class ReconcileInputs:
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]
    open_algo_orders: list[dict[str, Any]]
    income: list[dict[str, Any]]
    time_offset_ms: int | None
    optional_errors: dict[str, str]


async def fetch_reconcile_inputs(rest: Any, income_start: int) -> ReconcileInputs:
    """Fetch authoritative trading state; auxiliary failures stay non-fatal."""
    optional_errors: dict[str, str] = {}
    time_offset: int | None = None
    try:
        time_offset = int(await asyncio.to_thread(rest.sync_time))
    except Exception as exc:
        optional_errors["time_sync"] = f"{type(exc).__name__}: {exc}"[:400]

    # One worker performs these sequentially so its HTTP Session can reuse the
    # same TLS connection and the snapshot cannot fail because income history
    # or clock telemetry was temporarily unavailable.
    snapshot = await asyncio.to_thread(rest.reconcile_snapshot)

    income: list[dict[str, Any]] = []
    try:
        income = list(await asyncio.to_thread(rest.income, income_start, 1000))
    except Exception as exc:
        optional_errors["income"] = f"{type(exc).__name__}: {exc}"[:400]

    return ReconcileInputs(
        account=dict(snapshot.get("account") or {}),
        positions=list(snapshot.get("positions") or []),
        open_orders=list(snapshot.get("open_orders") or []),
        open_algo_orders=list(snapshot.get("open_algo_orders") or []),
        income=income,
        time_offset_ms=time_offset,
        optional_errors=optional_errors,
    )


def recoverable_connectivity_halts(
    reason_text: str,
    *,
    private_stream_confirmed: bool = True,
) -> set[str]:
    """Return only halts that an authoritative reconnect can prove recovered."""
    return {
        reason
        for reason in str(reason_text or "").split(" | ")
        if reason.startswith("reconcile_failed_3x:")
        or (
            private_stream_confirmed
            and (
                reason == "listenkeyexpired"
                or reason.startswith("private_stream_failed:")
            )
        )
    }


def _intent_id(signal: WaterfallSignal) -> str:
    raw = f"{signal.strategy}|{signal.signal_id}|{signal.action}|{signal.decision_time}"
    return f"intent-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:28]}"


def signal_to_intent(signal: WaterfallSignal) -> TradeIntent:
    if signal.action == "open_short":
        action = IntentAction.OPEN_SHORT
        reason = "strategy_entry"
    else:
        action = IntentAction.CLOSE_SHORT
        reason = next(
            (item.split("=", 1)[1] for item in signal.evidence if item.startswith("exit_reason=")),
            signal.action,
        )
    return TradeIntent(
        intent_id=_intent_id(signal),
        signal_id=signal.signal_id,
        position_id=signal.position_id,
        strategy=signal.strategy,
        symbol=signal.symbol,
        action=action,
        decision_time=signal.decision_time,
        signal_price=D(str(signal.price)),
        strategy_stop_price=D(str(signal.stop_price)),
        reason=reason,
        evidence=tuple(signal.evidence),
    )


def sanitized_preflight(
    config: LiveTradingConfig,
    credentials: BinanceCredentials,
    state: dict[str, Any],
    rules: ExchangeRules,
) -> dict[str, Any]:
    account = state.get("account") or {}
    positions = [
        row for row in state.get("positions", [])
        if D(str(row.get("positionAmt") or "0")) != 0
    ]
    return {
        "ok": True,
        "mode": config.mode,
        "account_api": config.account_api,
        "position_mode": config.position_mode,
        "real_order_enabled": config.real_order_enabled,
        "orders_authorized": False,
        "api_key": credentials.masked_key,
        "time_offset_ms": state.get("time_offset_ms"),
        "one_way_mode": not bool((state.get("position_mode") or {}).get("dualSidePosition")),
        "wallet_balance_usdt": account.get("totalWalletBalance", "0"),
        "available_balance_usdt": account.get("availableBalance", "0"),
        "open_positions": len(positions),
        "open_orders": len(state.get("open_orders") or []),
        "open_algo_orders": len(state.get("open_algo_orders") or []),
        "exchange_symbols": len(rules.symbols),
    }


@dataclass
class LiveRuntime:
    config: LiveTradingConfig
    credentials: BinanceCredentials
    gateway: BinanceGateway
    ledger: LiveLedger
    rules: ExchangeRules
    oms: LiveOrderManager
    exchange_state: dict[str, Any]

    async def close(self) -> None:
        await self.gateway.close()


async def build_runtime(
    settings: dict[str, Any],
    *,
    mode_override: str | None = None,
    max_notional_override: float | None = None,
    orders_authorized: bool = False,
) -> LiveRuntime:
    config = LiveTradingConfig.from_settings(settings, mode_override, max_notional_override)
    credentials = BinanceCredentials.from_env()
    gateway = BinanceGateway(config, credentials)
    try:
        state = await gateway.preflight_connect(
            connect_trade_ws=config.sends_real_orders and orders_authorized,
        )
    except Exception:
        await gateway.close()
        raise
    exchange_hedge = bool((state.get("position_mode") or {}).get("dualSidePosition"))
    if config.position_mode == "one_way" and exchange_hedge:
        await gateway.close()
        raise RuntimeError("Binance account must use one-way position mode")
    if config.position_mode == "hedge" and not exchange_hedge:
        await gateway.close()
        raise RuntimeError("Binance account must use hedge position mode")
    exchange_info = await asyncio.to_thread(gateway.rest.exchange_info)
    rules = ExchangeRules(exchange_info)
    ledger = LiveLedger(config.ledger_path)
    oms = LiveOrderManager(
        config, gateway, ledger, rules, orders_authorized=orders_authorized,
    )
    snapshot = account_snapshot_from_api(state.get("account") or {}, utc_ms())
    oms.set_account(snapshot, state.get("account") or {})
    if config.sends_real_orders and orders_authorized:
        await oms.recover_inflight_orders()
    await oms.reconcile(state)
    return LiveRuntime(config, credentials, gateway, ledger, rules, oms, state)


async def live_preflight(
    settings: dict[str, Any],
    *,
    mode_override: str | None = None,
    max_notional_override: float | None = None,
) -> dict[str, Any]:
    runtime = await build_runtime(
        settings,
        mode_override=mode_override,
        max_notional_override=max_notional_override,
        orders_authorized=False,
    )
    try:
        result = sanitized_preflight(
            runtime.config, runtime.credentials, runtime.exchange_state, runtime.rules,
        )
        result["safe_halt_reason"] = runtime.oms.safe_halt_reason
        result["ok"] = not bool(runtime.oms.safe_halt_reason)
        return result
    finally:
        await runtime.close()


def issue_order_nonce(ledger_path: str | Path, ttl_seconds: int = 300) -> dict[str, Any]:
    ledger = LiveLedger(ledger_path)
    nonce = secrets.token_urlsafe(18)
    expires_at = int(time.time()) + max(30, int(ttl_seconds))
    ledger.set_meta("real_order_nonce_hash", hashlib.sha256(nonce.encode("utf-8")).hexdigest(), utc_ms())
    ledger.set_meta("real_order_nonce_expires", str(expires_at), utc_ms())
    ledger.set_meta("real_order_nonce_used", "0", utc_ms())
    return {"nonce": nonce, "expires_at": expires_at}


def consume_order_nonce(ledger_path: str | Path, nonce: str) -> bool:
    ledger = LiveLedger(ledger_path)
    supplied = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    expected = ledger.get_meta("real_order_nonce_hash")
    if not expected or not secrets.compare_digest(expected, supplied):
        return False
    return ledger.consume_nonce(supplied, int(time.time()), utc_ms())


class ClaudeLiveTradingService:
    def __init__(
        self,
        settings: dict[str, Any],
        runtime: LiveRuntime,
        *,
        broad_top: int | None = None,
        max_workers: int | None = None,
    ):
        self.settings = settings
        self.runtime = runtime
        self.dirs = ensure_dirs(settings)
        raw = dict(settings.get("live_trading") or {})
        configured_strategy_path = raw.get("strategy_state_path")
        strategy_path = (
            resolve_path(configured_strategy_path)
            if configured_strategy_path
            else runtime.config.ledger_path.with_name("live_strategy.db")
        )
        paper_path = resolve_path((settings.get("paths") or {}).get("db_path", "storage/hunter.db"))
        resolved_paths = {
            "live_ledger": runtime.config.ledger_path.resolve(),
            "live_strategy": strategy_path.resolve(),
            "paper": paper_path.resolve(),
        }
        if len(set(resolved_paths.values())) != len(resolved_paths):
            raise RuntimeError(f"live databases must be isolated: {resolved_paths}")
        self.strategy_store = Store(strategy_path)
        strategy_settings = json.loads(json.dumps(settings))
        strategy_settings["paths"]["db_path"] = str(strategy_path)
        self.engine = BoardWaterfallEngine(strategy_settings)
        self.engine.load_positions(self.strategy_store.active_waterfall_positions(strategy=STRATEGY_NAME))
        self.engine.load_recent_state(
            self.strategy_store.waterfall_position_rows(limit=0, strategy=STRATEGY_NAME),
            self.strategy_store.waterfall_signal_rows(limit=1000, strategy=STRATEGY_NAME),
        )
        wf = settings.get("waterfall_quant") or {}
        self.broad_top = int(broad_top or raw.get("broad_top") or wf.get("broad_top") or 400)
        self.max_workers = int(max_workers or raw.get("max_workers") or wf.get("max_workers") or 3)
        self.discover_every = parse_duration_seconds(str(raw.get("discover_every") or wf.get("discover_every") or "15m"))
        self.source: WebSocketMarketSource | None = None
        self._private_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._oms_lock = asyncio.Lock()
        self.notifier = LiveEventNotifier(settings, runtime.config)
        self._last_heartbeat_ms = 0
        self._processed_events = 0
        self._consecutive_reconcile_failures = 0
        self._optional_reconcile_failures: set[str] = set()
        self._gap_repair_next_attempt_ms: dict[str, int] = {}
        if self._is_actual_execution:
            self._reconcile_strategy_positions_with_live("startup")

    @property
    def _is_actual_execution(self) -> bool:
        return self.runtime.config.sends_real_orders and self.runtime.oms.orders_authorized

    def _has_pending_entry(self, symbol: str) -> bool:
        terminal = {
            OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED,
            OrderState.EXCHANGE_REJECTED, OrderState.RISK_REJECTED, OrderState.CLOSED,
        }
        return any(
            order.symbol == symbol
            and order.side == "SELL"
            and not order.reduce_only
            and order.state not in terminal
            for order in self.runtime.oms.orders.values()
        )

    def _reconcile_strategy_positions_with_live(self, reason: str) -> None:
        if not self._is_actual_execution:
            return
        live_symbols = set(self.runtime.oms.positions_by_symbol)
        strategy_symbols = set(self.engine.positions)
        for symbol in sorted(strategy_symbols - live_symbols):
            if self._has_pending_entry(symbol):
                continue
            position = self.engine.positions.pop(symbol)
            position.status = "execution_absent"
            position.exit_time = utc_ms()
            position.exit_reason = f"live_position_absent_{reason}"
            position.updated_time = position.exit_time
            self.strategy_store.upsert_waterfall_position(position.to_dict())
        missing_strategy = live_symbols - set(self.engine.positions)
        if missing_strategy:
            self.runtime.oms.safe_halt(
                f"strategy_state_missing_for_live_positions:{sorted(missing_strategy)}"
            )
        mismatched_ids = sorted(
            symbol for symbol in live_symbols & set(self.engine.positions)
            if self.runtime.oms.positions_by_symbol[symbol].position_id
            != self.engine.positions[symbol].position_id
        )
        if mismatched_ids:
            self.runtime.oms.safe_halt(
                f"strategy_live_position_id_mismatch:{mismatched_ids}"
            )
        for symbol in sorted(live_symbols & set(self.engine.positions)):
            live_position = self.runtime.oms.positions_by_symbol[symbol]
            strategy_position = self.engine.positions[symbol]
            if live_position.position_id != strategy_position.position_id:
                continue
            marker = "live_execution_synced"
            if marker not in strategy_position.evidence:
                strategy_position.entry_price = float(live_position.entry_price)
                strategy_position.best_price = min(
                    float(strategy_position.best_price), float(live_position.entry_price)
                )
                strategy_position.worst_price = max(
                    float(strategy_position.worst_price), float(live_position.entry_price)
                )
                strategy_position.evidence.append(marker)
            strategy_position.status = "open"
            strategy_position.stop_price = float(live_position.structure_stop_price)
            strategy_position.notional_usdt = float(
                live_position.entry_price * live_position.quantity
            )
            strategy_position.margin_usdt = (
                strategy_position.notional_usdt / self.runtime.config.leverage
            )
            strategy_position.leverage = float(self.runtime.config.leverage)
            strategy_position.updated_time = utc_ms()
            self.strategy_store.upsert_waterfall_position(strategy_position.to_dict())

    async def _clear_recovered_connectivity_halts(
        self,
        source: str,
        *,
        private_stream_confirmed: bool,
    ) -> list[str]:
        recoverable = recoverable_connectivity_halts(
            self.runtime.oms.safe_halt_reason,
            private_stream_confirmed=private_stream_confirmed,
        )
        if not recoverable:
            return []
        cleared = self.runtime.oms.clear_safe_halt_reasons(recoverable)
        if not cleared:
            return []
        self.runtime.ledger.append_event(
            utc_ms(),
            "CONNECTIVITY_AUTO_RECOVERED",
            source,
            {
                "source": source,
                "cleared": cleared,
                "remaining": self.runtime.oms.safe_halt_reason,
            },
        )
        print(
            f"live connectivity recovered source={source} cleared={cleared} "
            f"remaining={self.runtime.oms.safe_halt_reason!r}",
            flush=True,
        )
        if not self.runtime.oms.safe_halt_reason:
            await asyncio.to_thread(self.notifier.recovered, cleared)
        return cleared

    def _heartbeat(self, status: str, processed: int | None = None, *, force: bool = False) -> None:
        stamp = utc_ms()
        if processed is not None:
            self._processed_events = max(self._processed_events, int(processed))
        if not force and stamp - self._last_heartbeat_ms < 5_000:
            return
        self._last_heartbeat_ms = stamp
        self.runtime.ledger.set_meta("service_heartbeat_time", str(stamp), stamp)
        self.runtime.ledger.set_meta("service_status", status, stamp)
        self.runtime.ledger.set_meta("service_pid", str(os.getpid()), stamp)
        self.runtime.ledger.set_meta(
            "service_processed_events", str(self._processed_events), stamp,
        )

    def _sync_execution_outcome(
        self,
        signal: WaterfallSignal,
        strategy_position: Any | None,
        result: dict[str, Any],
    ) -> None:
        if not self._is_actual_execution or strategy_position is None:
            return
        live_position = self.runtime.oms.positions_by_symbol.get(signal.symbol)
        if signal.action == "open_short":
            if live_position and live_position.position_id == signal.position_id:
                strategy_position.status = "open"
                strategy_position.entry_price = float(live_position.entry_price)
                strategy_position.best_price = float(live_position.entry_price)
                strategy_position.worst_price = float(live_position.entry_price)
                strategy_position.stop_price = float(live_position.structure_stop_price)
                strategy_position.notional_usdt = float(
                    live_position.entry_price * live_position.quantity
                )
                strategy_position.margin_usdt = (
                    strategy_position.notional_usdt / self.runtime.config.leverage
                )
                strategy_position.leverage = float(self.runtime.config.leverage)
                strategy_position.updated_time = utc_ms()
                self.engine.positions[signal.symbol] = strategy_position
                self.strategy_store.upsert_waterfall_position(strategy_position.to_dict())
                return
            status = str(result.get("status") or "")
            if status not in {"unknown", "cancel_unknown"} and not self._has_pending_entry(signal.symbol):
                current = self.engine.positions.get(signal.symbol)
                if current and current.position_id == signal.position_id:
                    self.engine.positions.pop(signal.symbol, None)
                strategy_position.status = "execution_rejected"
                strategy_position.exit_time = utc_ms()
                strategy_position.exit_reason = f"live_entry_{status or 'failed'}"
                strategy_position.updated_time = strategy_position.exit_time
                self.strategy_store.upsert_waterfall_position(strategy_position.to_dict())
            return
        if live_position:
            reverted_pnl = float(getattr(strategy_position, "pnl_usdt", 0.0) or 0.0)
            self.engine.realized_pnl_usdt -= reverted_pnl
            strategy_position.status = "open"
            strategy_position.exit_time = None
            strategy_position.exit_price = 0.0
            strategy_position.pnl_pct = 0.0
            strategy_position.pnl_usdt = 0.0
            strategy_position.exit_reason = ""
            strategy_position.updated_time = utc_ms()
            self.engine.positions[signal.symbol] = strategy_position
            self.strategy_store.upsert_waterfall_position(strategy_position.to_dict())

    async def _fetch_execution_market(self, symbol: str) -> tuple[Any, dict[str, Any]]:
        quote_row, depth = await asyncio.gather(
            asyncio.to_thread(self.runtime.gateway.rest.book_ticker, symbol),
            asyncio.to_thread(self.runtime.gateway.rest.depth, symbol, 20),
        )
        return quote_from_api(quote_row), depth

    async def _handle_signal(
        self, signal: WaterfallSignal, strategy_position: Any | None = None
    ) -> dict[str, Any]:
        intent = signal_to_intent(signal)
        try:
            quote, depth = await self._fetch_execution_market(signal.symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Market snapshots are read-only and no intent has reached the OMS.
            # A network flap is therefore retryable and must not persist a
            # manual SAFE_HALT.
            detail = f"{type(exc).__name__}: {exc}"[:300]
            self.runtime.ledger.append_event(
                utc_ms(),
                "EXECUTION_MARKET_UNAVAILABLE",
                intent.intent_id,
                {"signal_id": signal.signal_id, "symbol": signal.symbol, "detail": detail},
            )
            return {
                "status": "market_unavailable",
                "error": type(exc).__name__,
                "detail": detail,
            }
        return await self._execute_signal_with_market(
            signal,
            quote,
            depth,
            strategy_position,
            intent=intent,
        )

    async def _execute_signal_with_market(
        self,
        signal: WaterfallSignal,
        quote: Any,
        depth: dict[str, Any],
        strategy_position: Any | None = None,
        *,
        intent: TradeIntent | None = None,
    ) -> dict[str, Any]:
        intent = intent or signal_to_intent(signal)
        try:
            async with self._oms_lock:
                result = await self.runtime.oms.handle_intent(intent, quote, depth)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.runtime.oms.safe_halt(f"intent_execution_failed:{type(exc).__name__}")
            result = {"status": "execution_error", "error": type(exc).__name__}
        if self.runtime.oms.account is not None:
            result = {
                **result,
                "account": self.runtime.oms.account.to_dict(),
            }
        self._sync_execution_outcome(signal, strategy_position, result)
        self.runtime.ledger.append_event(
            utc_ms(), "STRATEGY_INTENT_RESULT", intent.intent_id,
            {"signal_id": signal.signal_id, "result": result},
        )
        pushed, push_error = await asyncio.to_thread(self.notifier.intent_result, intent, result)
        self.runtime.ledger.append_event(
            utc_ms(), "LIVE_NOTIFICATION", intent.intent_id,
            {"pushed": pushed, "error": "" if pushed else push_error},
        )
        return result

    async def _sync_trailing_protection(self, symbol: str) -> None:
        state = self.engine.live_protection_state(symbol)
        live_position = self.runtime.oms.positions_by_symbol.get(symbol)
        if not state or not live_position:
            return
        desired = D(str(state["trail_price"]))
        arm = bool(state["arm_trail"])
        if arm:
            quote, _depth = await self._fetch_execution_market(symbol)
            if quote.ask_price >= desired:
                intent = TradeIntent(
                    intent_id=f"protective-exit-{live_position.position_id}-{state['decision_time']}",
                    signal_id=f"protective-exit-{symbol}-{state['decision_time']}",
                    position_id=live_position.position_id,
                    strategy=STRATEGY_NAME,
                    symbol=symbol,
                    action=IntentAction.CLOSE_SHORT,
                    decision_time=int(state["decision_time"]),
                    signal_price=quote.ask_price,
                    strategy_stop_price=live_position.structure_stop_price,
                    reason="trailing_price_already_crossed",
                    evidence=("live_protection_race_guard",),
                )
                async with self._oms_lock:
                    await self.runtime.oms.handle_intent(intent, quote, _depth)
                return
        async with self._oms_lock:
            await self.runtime.oms.update_trail(
                live_position.position_id, desired, arm, int(state["decision_time"]),
            )

    def _entry_history_repair_needed(self, candle: Candle) -> bool:
        """Limit REST gap repair to live risk or a genuine board candidate."""
        if candle.symbol in self.engine.positions:
            return True
        values = list(self.engine.candles.get(candle.symbol, ()))
        ref_open_time = candle.open_time - 1440 * MINUTE_MS
        reference = next(
            (item for item in values if item.open_time == ref_open_time), None
        )
        if reference is None or reference.close <= 0:
            return False
        return (
            candle.close / reference.close - 1.0
            >= float(self.engine.cfg["min_ret_24h"])
        )

    async def _repair_entry_history_if_needed(
        self,
        event: KlineClosed,
        client: BinanceRestClient,
    ) -> bool:
        """Backfill a websocket hole before evaluating a current live candle.

        A strict 1,441-bar board window is intentional. A single dropped
        websocket minute must therefore be repaired, not silently converted
        into a relaxed signal or a missed monitoring day. Historical repairs
        only prime state; they never invoke the OMS. The current received
        candle remains the only possible source of a real order.
        """
        candle = event.candle
        if not self._entry_history_repair_needed(candle):
            return True
        missing = missing_entry_history_opens(
            list(self.engine.candles.get(candle.symbol, ())), candle.open_time,
        )
        if not missing:
            return True
        now = utc_ms()
        retry_at = self._gap_repair_next_attempt_ms.get(candle.symbol, 0)
        if now < retry_at:
            return False
        first_missing = missing[0]
        last_missing = missing[-1]
        span = (last_missing - first_missing) // MINUTE_MS + 1
        # Binance supports at most 1,500 rows. A wider hole means the full
        # 24h entry window is missing, so request that window once instead.
        if span > 1500:
            first_missing = candle.open_time - 1440 * MINUTE_MS
            last_missing = candle.open_time - MINUTE_MS
            span = 1440
        try:
            fetched = await asyncio.to_thread(
                client.klines,
                candle.symbol,
                "1m",
                int(max(1, min(1500, span))),
                int(first_missing),
                int(last_missing + MINUTE_MS - 1),
            )
        except Exception as exc:
            self._gap_repair_next_attempt_ms[candle.symbol] = now + MINUTE_MS
            detail = f"{type(exc).__name__}: {exc}"[:300]
            self.runtime.ledger.append_event(
                now,
                "MARKET_CANDLE_GAP_UNRESOLVED",
                candle.symbol,
                {
                    "missing_minutes": len(missing),
                    "first_open_time": first_missing,
                    "last_open_time": last_missing,
                    "error": detail,
                },
            )
            print(
                f"live candle gap repair failed symbol={candle.symbol} "
                f"missing={len(missing)} error={detail}",
                flush=True,
            )
            return False

        wanted = set(missing)
        repaired = [
            row for row in fetched
            if row.open_time in wanted and row.open_time < candle.open_time
        ]
        if repaired:
            self.strategy_store.save_candles(repaired)
            watch_rows = self.engine.prime_candles(repaired)
            if watch_rows:
                self.strategy_store.upsert_waterfall_watch(watch_rows[-1:])
        remaining = missing_entry_history_opens(
            list(self.engine.candles.get(candle.symbol, ())), candle.open_time,
        )
        if remaining:
            self._gap_repair_next_attempt_ms[candle.symbol] = now + MINUTE_MS
            self.runtime.ledger.append_event(
                now,
                "MARKET_CANDLE_GAP_UNRESOLVED",
                candle.symbol,
                {
                    "missing_minutes": len(remaining),
                    "first_open_time": remaining[0],
                    "last_open_time": remaining[-1],
                    "fetched_minutes": len(repaired),
                },
            )
            print(
                f"live candle gap remains symbol={candle.symbol} "
                f"missing={len(remaining)} fetched={len(repaired)}",
                flush=True,
            )
            return False
        self._gap_repair_next_attempt_ms.pop(candle.symbol, None)
        self.runtime.ledger.append_event(
            now,
            "MARKET_CANDLE_GAP_REPAIRED",
            candle.symbol,
            {
                "missing_minutes": len(missing),
                "first_open_time": missing[0],
                "last_open_time": missing[-1],
                "fetched_minutes": len(repaired),
            },
        )
        print(
            f"live candle gap repaired symbol={candle.symbol} "
            f"missing={len(missing)}",
            flush=True,
        )
        return True

    async def _private_events(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                async for payload in self.runtime.gateway.user_stream.events():
                    async with self._oms_lock:
                        await self.runtime.oms.handle_user_event(payload)
                    event_type = str(payload.get("e") or "")
                    if event_type == "listenKeyExpired":
                        # The exchange can leave the expired socket readable.
                        # Force the reconnect path instead of remaining halted on
                        # a stream that can no longer deliver account events.
                        raise RuntimeError("listen key expired")
                    order_data = payload.get("o") or {}
                    entry_fill = (
                        event_type == "ORDER_TRADE_UPDATE"
                        and str(order_data.get("S") or "") == "SELL"
                        and str(order_data.get("x") or "") == "TRADE"
                    )
                    exit_fill = (
                        event_type == "ORDER_TRADE_UPDATE"
                        and str(order_data.get("S") or "") == "BUY"
                        and str(order_data.get("x") or "") == "TRADE"
                    )
                    risk_event = event_type in {"MARGIN_CALL", "riskLevelChange"}
                    if risk_event:
                        await asyncio.to_thread(
                            self.notifier.safe_halt, self.runtime.oms.safe_halt_reason,
                        )
                    if event_type in {
                        "ALGO_UPDATE", "listenKeyExpired", "MARGIN_CALL", "riskLevelChange",
                    } or entry_fill or exit_fill:
                        if event_type == "ALGO_UPDATE":
                            await asyncio.sleep(0.1)
                        await self._reconcile_once()
                raise RuntimeError("private stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"private_stream_failed:{type(exc).__name__}"
                self.runtime.oms.safe_halt(reason)
                await asyncio.to_thread(self.notifier.safe_halt, self.runtime.oms.safe_halt_reason)
                await self.runtime.gateway.user_stream.close()
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)
                try:
                    await self.runtime.gateway.user_stream.connect()
                    await self._reconcile_once()
                    await self._clear_recovered_connectivity_halts(
                        "private_stream_reconnect", private_stream_confirmed=True,
                    )
                    delay = 1.0
                except Exception:
                    continue

    async def _reconcile_once(self) -> None:
        day_start = utc_ms() // 86_400_000 * 86_400_000
        prior_income_sync = int(
            self.runtime.ledger.get_meta("last_income_sync_time", "0") or 0
        )
        income_start = (
            max(0, prior_income_sync - 60_000)
            if prior_income_sync > 0
            else day_start
        )
        inputs = await fetch_reconcile_inputs(self.runtime.gateway.rest, income_start)
        account = inputs.account
        positions = inputs.positions
        orders = inputs.open_orders
        algos = inputs.open_algo_orders
        income = inputs.income
        if inputs.time_offset_ms is not None:
            self.runtime.gateway.trade_ws.time_offset_ms = inputs.time_offset_ms
        current_optional = set(inputs.optional_errors)
        for name, detail in inputs.optional_errors.items():
            if name not in self._optional_reconcile_failures:
                print(f"live optional reconcile failure endpoint={name} detail={detail}", flush=True)
            self.runtime.ledger.append_event(
                utc_ms(), "RECONCILE_OPTIONAL_FAILED", name, {"endpoint": name, "detail": detail},
            )
        recovered_optional = self._optional_reconcile_failures - current_optional
        for name in sorted(recovered_optional):
            print(f"live optional reconcile recovered endpoint={name}", flush=True)
            self.runtime.ledger.append_event(
                utc_ms(), "RECONCILE_OPTIONAL_RECOVERED", name, {"endpoint": name},
            )
        self._optional_reconcile_failures = current_optional
        self.runtime.oms.set_account(account_snapshot_from_api(account, utc_ms()), account)
        if "income" not in current_optional:
            self.runtime.ledger.save_income(income)
            self.runtime.ledger.set_meta("last_income_sync_time", str(utc_ms()), utc_ms())
        self.runtime.oms.refresh_sizing_state(initialize=self.runtime.oms.orders_authorized)
        sizing_start = self.runtime.oms.sizing_start_time
        loss_window_start = max(day_start, sizing_start) if sizing_start > 0 else day_start
        trading_pnl = self.runtime.ledger.trading_income_since(loss_window_start)
        margin_balance = D(str(account.get("totalMarginBalance") or account.get("totalWalletBalance") or "0"))
        strategy_equity = self.runtime.oms.risk.sizing_equity
        loss_base = strategy_equity if strategy_equity > 0 else margin_balance
        loss_limit = loss_base * D(str(self.runtime.config.daily_loss_limit_pct))
        if trading_pnl < -loss_limit:
            self.runtime.oms.safe_halt(f"daily_loss_limit:pnl={trading_pnl}:limit={loss_limit}")
            await asyncio.to_thread(self.notifier.safe_halt, self.runtime.oms.safe_halt_reason)
        exchange_state = {
            "positions": positions,
            "open_orders": orders,
            "open_algo_orders": algos,
        }
        async with self._oms_lock:
            await self.runtime.oms.reconcile(exchange_state)
        operational = self.runtime.oms.recoverable_halts_after_reconcile(
            exchange_state
        )
        if operational:
            cleared = self.runtime.oms.clear_safe_halt_reasons(operational)
            self.runtime.ledger.append_event(
                utc_ms(),
                "OPERATIONAL_HALT_AUTO_RECOVERED",
                "reconcile",
                {
                    "cleared": cleared,
                    "remaining": self.runtime.oms.safe_halt_reason,
                },
            )
            if cleared and not self.runtime.oms.safe_halt_reason:
                await asyncio.to_thread(self.notifier.recovered, cleared)
        self._reconcile_strategy_positions_with_live("periodic_reconcile")
        self._heartbeat("running")

    async def _periodic_reconcile(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.runtime.config.reconcile_interval_seconds)
            try:
                await self._reconcile_once()
                self._consecutive_reconcile_failures = 0
                await self._clear_recovered_connectivity_halts(
                    "periodic_reconcile", private_stream_confirmed=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_reconcile_failures += 1
                code = getattr(exc, "code", None)
                status = getattr(exc, "status", None)
                endpoint = getattr(exc, "endpoint", "") or "critical_snapshot"
                detail = str(exc).replace("\n", " ")[:300]
                print(
                    f"live reconcile failure {self._consecutive_reconcile_failures}/3 "
                    f"endpoint={endpoint} type={type(exc).__name__} "
                    f"code={code} status={status} detail={detail}",
                    flush=True,
                )
                self.runtime.ledger.append_event(
                    utc_ms(),
                    "RECONCILE_CRITICAL_FAILED",
                    endpoint,
                    {
                        "endpoint": endpoint,
                        "failure_count": self._consecutive_reconcile_failures,
                        "type": type(exc).__name__,
                        "code": code,
                        "status": status,
                        "detail": detail,
                    },
                )
                self._heartbeat("degraded", force=True)
                if self._consecutive_reconcile_failures >= 3:
                    reason = (
                        f"reconcile_failed_3x:{endpoint}:{type(exc).__name__}:"
                        f"code={code}:status={status}"
                    )
                    previous = self.runtime.oms.safe_halt_reason
                    self.runtime.oms.safe_halt(reason)
                    if self.runtime.oms.safe_halt_reason != previous:
                        await asyncio.to_thread(
                            self.notifier.safe_halt, self.runtime.oms.safe_halt_reason,
                        )

    async def run(self, samples: int = 0) -> None:
        public_client = BinanceRestClient(self.settings)
        self._heartbeat("starting", force=True)
        if self.runtime.config.sends_real_orders and self.runtime.oms.orders_authorized:
            await self.runtime.gateway.user_stream.connect()
            # A previous process may have persisted a transient stream halt.
            # A fresh private connection plus a complete exchange reconcile is
            # the evidence required to clear only those recoverable reasons.
            await self._reconcile_once()
            await self._clear_recovered_connectivity_halts(
                "startup_reconcile", private_stream_confirmed=True,
            )
            self._private_task = asyncio.create_task(self._private_events(), name="live-private-events")
            self._reconcile_task = asyncio.create_task(self._periodic_reconcile(), name="live-reconcile")
        symbols = await asyncio.to_thread(
            refresh_waterfall_universe,
            public_client, self.strategy_store, self.engine, self.settings,
            self.broad_top, self.max_workers,
        )
        self.source = WebSocketMarketSource(self.settings, symbols, ["1m"])
        processed = 0
        next_discovery = utc_ms() + self.discover_every * 1000
        agen = self.source.events()
        try:
            while samples <= 0 or processed < samples:
                timeout = max(1.0, (next_discovery - utc_ms()) / 1000.0)
                try:
                    event = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    refreshed_symbols = await asyncio.to_thread(
                        refresh_waterfall_universe,
                        public_client, self.strategy_store, self.engine, self.settings,
                        self.broad_top, self.max_workers,
                    )
                    if universe_requires_stream_rebuild(symbols, refreshed_symbols):
                        await self.source.close()
                        await agen.aclose()
                        self.source = WebSocketMarketSource(
                            self.settings, refreshed_symbols, ["1m"],
                        )
                        agen = self.source.events()
                        print(
                            "live market universe changed; rebuilt websocket "
                            f"symbols={len(refreshed_symbols)}",
                            flush=True,
                        )
                    else:
                        print(
                            "live market universe unchanged; preserving websocket "
                            f"symbols={len(refreshed_symbols)}",
                            flush=True,
                        )
                    symbols = refreshed_symbols
                    next_discovery = utc_ms() + self.discover_every * 1000
                    continue
                processed += 1
                self._heartbeat("running", processed)
                await self._repair_entry_history_if_needed(event, public_client)
                self.strategy_store.save_candles([event.candle])
                watch, positions, signals = self.engine.on_kline(event)
                self.strategy_store.upsert_waterfall_watch(watch)
                for position in positions:
                    self.strategy_store.upsert_waterfall_position(position.to_dict())
                for signal in signals:
                    self.strategy_store.save_waterfall_signal(signal.to_dict(), pushed=False, push_error="live_service")
                    strategy_position = next(
                        (position for position in positions if position.position_id == signal.position_id),
                        None,
                    )
                    await self._handle_signal(signal, strategy_position)
                if not signals:
                    await self._sync_trailing_protection(event.symbol)
        finally:
            self._stop.set()
            self._heartbeat("stopping", processed, force=True)
            if self.source:
                await self.source.close()
            for task in (self._private_task, self._reconcile_task):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self._private_task, self._reconcile_task) if task),
                return_exceptions=True,
            )
            await self.runtime.close()


class SharedPaperSignalLiveTradingService(ClaudeLiveTradingService):
    """Execute the paper monitor's durable Claude signal stream.

    The paper monitor remains the only public-market consumer and the only
    strategy engine. This process owns only private account/order streams,
    exchange execution and protection orders.
    """

    CURSOR_META_KEY = "shared_signal_cursor"
    CURSOR_READY_META_KEY = "shared_signal_cursor_initialized"
    SOURCE_DB_META_KEY = "shared_signal_source_db"
    SOURCE_STRATEGY_META_KEY = "shared_signal_strategy"

    def __init__(self, settings: dict[str, Any], runtime: LiveRuntime):
        # Intentionally do not call the standalone service constructor: it
        # creates another BoardWaterfallEngine, 400-symbol websocket and K-line
        # database, which is exactly the divergence this mode eliminates.
        self.settings = settings
        self.runtime = runtime
        self.signal_source = SharedPaperSignalSource(
            runtime.config.shared_signal_db_path,
            STRATEGY_NAME,
        )
        self._private_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._oms_lock = asyncio.Lock()
        self.notifier = LiveEventNotifier(settings, runtime.config)
        self._last_heartbeat_ms = 0
        self._processed_events = 0
        self._processed_signals = int(
            runtime.ledger.get_meta("service_processed_signals", "0") or 0
        )
        runtime.ledger.set_meta(
            "service_processed_signals",
            str(self._processed_signals),
            utc_ms(),
        )
        self._consecutive_reconcile_failures = 0
        self._optional_reconcile_failures: set[str] = set()
        self._cursor = SignalCursor()
        self._source_healthy = False
        self._source_health_initialized = False
        self._source_health_reason = "not_checked"
        self._protection_cache: dict[str, tuple[str, str, bool]] = {}
        self._source_db_failures = 0
        self._source_db_error = ""
        self._source_db_retry_at_ms = 0

    def _reconcile_strategy_positions_with_live(self, reason: str) -> None:
        # The shared paper engine owns strategy state. Exchange reconciliation
        # remains authoritative for actual positions and orders.
        return

    def _sync_execution_outcome(
        self,
        signal: WaterfallSignal,
        strategy_position: Any | None,
        result: dict[str, Any],
    ) -> None:
        return

    def _save_cursor(self, cursor: SignalCursor) -> None:
        stamp = utc_ms()
        self._cursor = cursor
        self.runtime.ledger.set_meta(self.CURSOR_META_KEY, cursor.to_json(), stamp)

    def _initialize_source_cursor(self) -> None:
        source_path = str(self.signal_source.db_path)
        prior_path = self.runtime.ledger.get_meta(self.SOURCE_DB_META_KEY)
        prior_strategy = self.runtime.ledger.get_meta(self.SOURCE_STRATEGY_META_KEY)
        if prior_path and Path(prior_path).resolve() != self.signal_source.db_path:
            raise RuntimeError(
                f"live signal source changed from {prior_path!r} to {source_path!r}"
            )
        if prior_strategy and prior_strategy != STRATEGY_NAME:
            raise RuntimeError(
                f"live signal strategy changed from {prior_strategy!r} to {STRATEGY_NAME!r}"
            )
        stamp = utc_ms()
        self.runtime.ledger.set_meta(self.SOURCE_DB_META_KEY, source_path, stamp)
        self.runtime.ledger.set_meta(self.SOURCE_STRATEGY_META_KEY, STRATEGY_NAME, stamp)
        if self.runtime.ledger.get_meta(self.CURSOR_READY_META_KEY) == "1":
            self._cursor = SignalCursor.from_json(
                self.runtime.ledger.get_meta(self.CURSOR_META_KEY)
            )
            return
        # First deployment starts at the end of the paper outbox. Historical
        # entries must never turn into real orders.
        self._save_cursor(self.signal_source.latest_cursor())
        self.runtime.ledger.set_meta(self.CURSOR_READY_META_KEY, "1", stamp)
        self.runtime.ledger.append_event(
            stamp,
            "SHARED_SIGNAL_CURSOR_INITIALIZED",
            STRATEGY_NAME,
            {
                "sequence": self._cursor.sequence,
                "decision_time": self._cursor.decision_time,
                "signal_id": self._cursor.signal_id,
                "source_db": source_path,
            },
        )

    def _source_health(self) -> tuple[bool, str, dict[str, Any]]:
        if self._source_db_error:
            return False, "source_db_unavailable", {
                "failure_count": self._source_db_failures,
                "retry_in_seconds": round(
                    max(0, self._source_db_retry_at_ms - utc_ms()) / 1000.0,
                    3,
                ),
                "error": self._source_db_error,
            }
        health = read_monitor_health(self.signal_source.db_path.parent)
        if not health:
            return False, "monitor_health_missing", {}
        now = utc_ms()
        age_seconds = max(0.0, (now - int(health.get("ts") or 0)) / 1000.0)
        candle_close = int(health.get("last_candle_close_ms") or 0)
        candle_age_seconds = (
            max(0.0, (now - candle_close) / 1000.0)
            if candle_close > 0
            else float("inf")
        )
        detail = {
            "monitor_age_seconds": round(age_seconds, 3),
            "candle_age_seconds": (
                round(candle_age_seconds, 3)
                if candle_age_seconds != float("inf")
                else None
            ),
            "universe": int(health.get("universe") or 0),
            "events": int(health.get("events") or 0),
        }
        stale = float(self.runtime.config.source_health_stale_seconds)
        if age_seconds > stale:
            return False, "monitor_heartbeat_stale", detail
        if candle_close <= 0 or candle_age_seconds > stale:
            return False, "closed_1m_candle_stale", detail
        if detail["universe"] <= 0:
            return False, "paper_universe_empty", detail
        return True, "", detail

    async def _set_source_health(
        self,
        healthy: bool,
        reason: str,
        detail: dict[str, Any],
    ) -> bool:
        changed = (
            not self._source_health_initialized
            or healthy != self._source_healthy
            or reason != self._source_health_reason
        )
        self._source_health_initialized = True
        self._source_healthy = healthy
        self._source_health_reason = reason
        if not changed:
            return healthy
        stamp = utc_ms()
        if healthy:
            self.runtime.ledger.append_event(
                stamp,
                "SHARED_SIGNAL_SOURCE_RECOVERED",
                STRATEGY_NAME,
                detail,
            )
            print(f"live shared signal source recovered detail={detail}", flush=True)
            await asyncio.to_thread(self.notifier.source_recovered, detail)
        else:
            self.runtime.ledger.append_event(
                stamp,
                "SHARED_SIGNAL_SOURCE_DEGRADED",
                STRATEGY_NAME,
                {"reason": reason, **detail},
            )
            print(
                f"live shared signal source degraded reason={reason} detail={detail}",
                flush=True,
            )
            await asyncio.to_thread(self.notifier.source_degraded, reason, detail)
        return healthy

    async def _refresh_source_health(self) -> bool:
        return await self._set_source_health(*self._source_health())

    async def _record_source_db_failure(
        self,
        operation: str,
        exc: BaseException,
    ) -> None:
        self.signal_source.close()
        self._source_db_failures += 1
        delay_seconds = min(30, 2 ** min(self._source_db_failures - 1, 5))
        self._source_db_retry_at_ms = utc_ms() + delay_seconds * 1000
        self._source_db_error = f"{type(exc).__name__}: {exc}"[:300]
        detail = {
            "operation": operation,
            "failure_count": self._source_db_failures,
            "retry_seconds": delay_seconds,
            "error": self._source_db_error,
        }
        self.runtime.ledger.append_event(
            utc_ms(),
            "SHARED_SIGNAL_SOURCE_READ_FAILED",
            operation,
            detail,
        )
        await self._set_source_health(False, "source_db_unavailable", detail)

    async def _clear_source_db_failure(self) -> bool:
        if not self._source_db_error:
            return self._source_healthy
        failures = self._source_db_failures
        self._source_db_failures = 0
        self._source_db_error = ""
        self._source_db_retry_at_ms = 0
        self.runtime.ledger.append_event(
            utc_ms(),
            "SHARED_SIGNAL_SOURCE_READ_RECOVERED",
            STRATEGY_NAME,
            {"prior_failure_count": failures},
        )
        return await self._refresh_source_health()

    def _skip_signal(self, sequence: int, signal: WaterfallSignal, reason: str) -> None:
        stamp = utc_ms()
        self.runtime.ledger.append_event(
            stamp,
            "SHARED_SIGNAL_SKIPPED",
            signal.signal_id,
            {
                "reason": reason,
                "action": signal.action,
                "symbol": signal.symbol,
                "decision_time": signal.decision_time,
            },
        )
        self._save_cursor(
            SignalCursor(sequence, signal.decision_time, signal.signal_id)
        )

    async def _poll_signals_once(self, source_healthy: bool) -> int:
        if self._source_db_error and utc_ms() < self._source_db_retry_at_ms:
            return 0
        try:
            signals = self.signal_source.signals_after(self._cursor, limit=100)
        except (sqlite3.Error, OSError) as exc:
            await self._record_source_db_failure("signals_after", exc)
            return 0
        if self._source_db_error:
            source_healthy = await self._clear_source_db_failure()
        handled = 0
        for sequence, signal in signals:
            if signal.action == "open_short":
                age_ms = max(0, utc_ms() - int(signal.decision_time))
                if age_ms > self.runtime.config.max_entry_signal_age_seconds * 1000:
                    self._skip_signal(sequence, signal, f"stale_entry:{age_ms}ms")
                    handled += 1
                    continue
                if not source_healthy:
                    # Never let a deferred risk-increasing intent block later
                    # exits in the ordered outbox. A fresh signal after source
                    # recovery will be handled normally; this one is discarded.
                    self._skip_signal(
                        sequence,
                        signal,
                        f"source_unhealthy:{self._source_health_reason}",
                    )
                    handled += 1
                    continue
                if self.runtime.oms.safe_halt_reason:
                    self._skip_signal(
                        sequence,
                        signal,
                        f"safe_halt:{self.runtime.oms.safe_halt_reason}",
                    )
                    handled += 1
                    continue
            result = await self._handle_signal(signal)
            if str(result.get("status") or "") == "market_unavailable":
                break
            self._save_cursor(
                SignalCursor(sequence, signal.decision_time, signal.signal_id)
            )
            self._processed_signals += 1
            self.runtime.ledger.set_meta(
                "service_processed_signals",
                str(self._processed_signals),
                utc_ms(),
            )
            handled += 1
        return handled

    async def _sync_shared_protection_once(self) -> None:
        if self._source_db_error:
            return
        try:
            states = {
                str(row["symbol"]): row
                for row in self.signal_source.protection_states()
            }
        except (sqlite3.Error, OSError) as exc:
            await self._record_source_db_failure("protection_states", exc)
            return
        live_symbols = set(self.runtime.oms.positions_by_symbol)
        self._protection_cache = {
            symbol: value
            for symbol, value in self._protection_cache.items()
            if symbol in live_symbols
        }
        for symbol, position in list(self.runtime.oms.positions_by_symbol.items()):
            state = states.get(symbol)
            if not state or str(state["position_id"]) != position.position_id:
                continue
            desired = D(str(state.get("trail_price") or "0"))
            arm = bool(state.get("arm_trail"))
            cache_value = (position.position_id, str(desired), arm)
            if self._protection_cache.get(symbol) == cache_value:
                continue
            decision_time = int(state.get("decision_time") or utc_ms())
            quote = None
            depth = None
            if arm and desired > 0:
                try:
                    quote, depth = await self._fetch_execution_market(symbol)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.runtime.ledger.append_event(
                        utc_ms(),
                        "PROTECTION_MARKET_UNAVAILABLE",
                        position.position_id,
                        {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:300]},
                    )
                    continue
                if quote.ask_price >= desired:
                    signal = WaterfallSignal(
                        signal_id=(
                            f"shared-protective-exit-{position.position_id}-{decision_time}"
                        ),
                        position_id=position.position_id,
                        symbol=symbol,
                        strategy=STRATEGY_NAME,
                        action="take_profit",
                        family="shared_live_protection",
                        rule="trailing_price_already_crossed",
                        decision_time=decision_time,
                        price=float(quote.ask_price),
                        stop_price=float(position.structure_stop_price),
                        evidence=["shared_paper_protection", "live_protection_race_guard"],
                    )
                    result = await self._execute_signal_with_market(
                        signal,
                        quote,
                        depth,
                    )
                    if str(result.get("status") or "") not in {
                        "execution_error",
                        "rejected",
                        "exchange_rejected",
                    }:
                        self._protection_cache[symbol] = cache_value
                    continue
            prior_halt = self.runtime.oms.safe_halt_reason
            async with self._oms_lock:
                update_result = await self.runtime.oms.update_trail(
                    position.position_id,
                    desired,
                    arm,
                    decision_time,
                )
            if str(update_result.get("status") or "") == "failed" and arm and desired > 0:
                handled = await self._recover_failed_trail_update(
                    position,
                    desired,
                    decision_time,
                    update_result,
                    fallback_quote=quote,
                    fallback_depth=depth,
                )
                if handled:
                    self._protection_cache[symbol] = cache_value
                    continue
            if self.runtime.oms.safe_halt_reason == prior_halt:
                self._protection_cache[symbol] = cache_value

    async def _recover_failed_trail_update(
        self,
        position: Any,
        desired: Decimal,
        decision_time: int,
        failed: dict[str, Any],
        *,
        fallback_quote: Any | None = None,
        fallback_depth: dict[str, Any] | None = None,
    ) -> bool:
        """Fail closed when a newly armed profit stop cannot be confirmed.

        A definite exchange rejection may be retried once with a fresh
        deterministic client id. An unknown execution is never resubmitted:
        after a fresh quote, either the desired stop has already been crossed
        or the position is closed to avoid leaving newly protected profit
        exposed with only the distant structure stop.
        """
        try:
            quote, depth = await self._fetch_execution_market(position.symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.runtime.ledger.append_event(
                utc_ms(),
                "TRAIL_FAILURE_MARKET_UNAVAILABLE",
                position.position_id,
                {"symbol": position.symbol, "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
            if fallback_quote is None or fallback_depth is None:
                return False
            quote, depth = fallback_quote, fallback_depth
            self.runtime.ledger.append_event(
                utc_ms(),
                "TRAIL_FAILURE_USING_PRIOR_MARKET",
                position.position_id,
                {
                    "symbol": position.symbol,
                    "quote_event_time": int(getattr(quote, "event_time", 0) or 0),
                },
            )

        old_protection = bool(failed.get("old_protection"))
        failure_kind = str(failed.get("failure_kind") or "")
        halt_reason = str(failed.get("halt_reason") or "")
        if (
            not old_protection
            and failure_kind == "GatewayError"
            and quote.ask_price < desired
        ):
            async with self._oms_lock:
                retry = await self.runtime.oms.update_trail(
                    position.position_id,
                    desired,
                    True,
                    decision_time + 1,
                )
            if str(retry.get("status") or "") in {
                "updated",
                "updated_with_old_cancel_unresolved",
                "unchanged",
            }:
                if halt_reason:
                    self.runtime.oms.clear_safe_halt_reasons({halt_reason})
                self.runtime.ledger.append_event(
                    utc_ms(),
                    "TRAIL_FIRST_ARM_RETRY_CONFIRMED",
                    position.position_id,
                    {"symbol": position.symbol, "desired_price": str(desired)},
                )
                return True

        if old_protection and quote.ask_price < desired:
            return False

        reason = (
            "trailing_price_already_crossed"
            if quote.ask_price >= desired
            else "trailing_first_arm_unavailable"
        )
        signal = WaterfallSignal(
            signal_id=f"trail-failsafe-exit-{position.position_id}-{decision_time}",
            position_id=position.position_id,
            symbol=position.symbol,
            strategy=STRATEGY_NAME,
            action="take_profit",
            family="shared_live_protection",
            rule=reason,
            decision_time=decision_time,
            price=float(quote.ask_price),
            stop_price=float(position.structure_stop_price),
            evidence=["shared_paper_protection", "live_trail_failure_failsafe"],
        )
        result = await self._execute_signal_with_market(signal, quote, depth)
        return str(result.get("status") or "") not in {
            "execution_error",
            "rejected",
            "exchange_rejected",
        }

    async def _start_private_supervision(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self.runtime.gateway.user_stream.connect()
                await self._reconcile_once()
                await self._clear_recovered_connectivity_halts(
                    "startup_reconcile",
                    private_stream_confirmed=True,
                )
                self._private_task = asyncio.create_task(
                    self._private_events(),
                    name="live-private-events",
                )
                self._reconcile_task = asyncio.create_task(
                    self._periodic_reconcile(),
                    name="live-reconcile",
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"[:300]
                self.runtime.ledger.append_event(
                    utc_ms(),
                    "PRIVATE_SUPERVISION_START_RETRY",
                    "startup",
                    {"detail": detail, "retry_seconds": delay},
                )
                self._heartbeat("private_stream_degraded", force=True)
                await self.runtime.gateway.user_stream.close()
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    async def _initialize_source_cursor_with_retry(self) -> None:
        delay = 1
        while not self._stop.is_set():
            try:
                self._initialize_source_cursor()
                await self._clear_source_db_failure()
                return
            except asyncio.CancelledError:
                raise
            except (sqlite3.Error, OSError) as exc:
                await self._record_source_db_failure("initialize_cursor", exc)
                self._heartbeat("source_degraded", force=True)
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def run(self, samples: int = 0) -> None:
        self._heartbeat("starting", force=True)
        await self._initialize_source_cursor_with_retry()
        if self._is_actual_execution:
            await self._start_private_supervision()
        loops = 0
        last_health_check = 0
        last_protection_sync = 0
        poll_seconds = self.runtime.config.signal_poll_interval_ms / 1000.0
        try:
            while samples <= 0 or loops < samples:
                loops += 1
                self._processed_events += 1
                now = utc_ms()
                if now - last_health_check >= 1_000:
                    await self._refresh_source_health()
                    last_health_check = now
                await self._poll_signals_once(self._source_healthy)
                if now - last_protection_sync >= 500:
                    await self._sync_shared_protection_once()
                    last_protection_sync = now
                for task in (self._private_task, self._reconcile_task):
                    if task and task.done() and not task.cancelled():
                        error = task.exception()
                        raise RuntimeError(
                            f"live supervision task stopped: {task.get_name()}: {error}"
                        )
                self._heartbeat(
                    "running" if self._source_healthy else "source_degraded",
                    self._processed_events,
                )
                await asyncio.sleep(poll_seconds)
        finally:
            self._stop.set()
            self._heartbeat("stopping", self._processed_events, force=True)
            self.signal_source.close()
            for task in (self._private_task, self._reconcile_task):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self._private_task, self._reconcile_task) if task),
                return_exceptions=True,
            )
