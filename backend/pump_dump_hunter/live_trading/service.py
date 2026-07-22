from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
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


D = Decimal


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
            async with self._oms_lock:
                result = await self.runtime.oms.handle_intent(intent, quote, depth)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.runtime.oms.safe_halt(f"intent_execution_failed:{type(exc).__name__}")
            result = {"status": "execution_error", "error": type(exc).__name__}
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
        async with self._oms_lock:
            await self.runtime.oms.reconcile({
                "positions": positions, "open_orders": orders, "open_algo_orders": algos,
            })
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
                    await self.source.close()
                    await agen.aclose()
                    symbols = await asyncio.to_thread(
                        refresh_waterfall_universe,
                        public_client, self.strategy_store, self.engine, self.settings,
                        self.broad_top, self.max_workers,
                    )
                    self.source = WebSocketMarketSource(self.settings, symbols, ["1m"])
                    agen = self.source.events()
                    next_discovery = utc_ms() + self.discover_every * 1000
                    continue
                processed += 1
                self._heartbeat("running", processed)
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
