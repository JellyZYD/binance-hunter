from __future__ import annotations

import asyncio
import hashlib
import json
import time
from decimal import Decimal
from typing import Any, Callable

from .config import LiveTradingConfig
from .exchange_rules import ExchangeRules, SymbolRules, decimal_text
from .execution_policy import ExecutionPolicyRouter, OrderPlan
from .gateway import BinanceGateway, GatewayError, UnknownExecutionStatus
from .ledger import LiveLedger
from .models import (
    AccountSnapshot,
    BookQuote,
    IntentAction,
    LiveFill,
    LiveOrder,
    LivePosition,
    OrderState,
    TradeIntent,
)
from .risk import LiveRiskManager


D = Decimal
TERMINAL_ORDER_STATES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def now_ms() -> int:
    return int(time.time() * 1000)


def short_id(prefix: str, value: str, suffix: str = "") -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"bh-{prefix}-{digest}{suffix}"[:32]


def quote_from_api(row: dict[str, Any], event_time: int | None = None) -> BookQuote:
    return BookQuote(
        symbol=str(row["symbol"]),
        bid_price=D(str(row["bidPrice"])),
        bid_quantity=D(str(row.get("bidQty") or "0")),
        ask_price=D(str(row["askPrice"])),
        ask_quantity=D(str(row.get("askQty") or "0")),
        event_time=int(event_time or row.get("time") or now_ms()),
    )


class LiveOrderManager:
    def __init__(
        self,
        config: LiveTradingConfig,
        gateway: BinanceGateway,
        ledger: LiveLedger,
        rules: ExchangeRules,
        *,
        orders_authorized: bool = False,
    ):
        self.config = config
        self.gateway = gateway
        self.ledger = ledger
        self.rules = rules
        self.orders_authorized = orders_authorized
        self.risk = LiveRiskManager(config)
        self.policy = ExecutionPolicyRouter(config)
        self.account: AccountSnapshot | None = None
        self.orders: dict[str, LiveOrder] = {}
        self.positions: dict[str, LivePosition] = {}
        self.positions_by_symbol: dict[str, LivePosition] = {}
        self.safe_halt_reason = ""
        self._configured_symbols: set[str] = set()
        self._intent_cache: dict[str, TradeIntent] = {}
        self._event_waiters: dict[str, asyncio.Event] = {}
        self.load_ledger_state()

    @property
    def can_send_orders(self) -> bool:
        return self.config.sends_real_orders and self.orders_authorized and not self.safe_halt_reason

    @property
    def can_send_reduce_orders(self) -> bool:
        # SAFE_HALT blocks exposure growth, never a reduce-only exit or an
        # exchange-hosted protection update for an existing position.
        return self.config.sends_real_orders and self.orders_authorized

    def _exchange_now_ms(self) -> int:
        offset = int(getattr(self.gateway.rest, "time_offset_ms", 0) or 0)
        return now_ms() + offset

    def load_ledger_state(self) -> None:
        self.safe_halt_reason = self.ledger.get_meta("safe_halt_reason")
        for row in self.ledger.pending_orders():
            order = LiveOrder(
                client_order_id=str(row["client_order_id"]), intent_id=str(row["intent_id"]),
                symbol=str(row["symbol"]), side=str(row["side"]), order_type=str(row["order_type"]),
                execution_policy=str(row["execution_policy"]), state=OrderState(str(row["state"])),
                quantity=D(str(row["quantity"])), price=D(str(row["price"])),
                reduce_only=bool(row["reduce_only"]), exchange_order_id=row["exchange_order_id"],
                filled_quantity=D(str(row["filled_quantity"])),
                applied_quantity=D(str(row.get("applied_quantity") or "0")),
                applied_notional=D(str(row.get("applied_notional") or "0")),
                average_price=D(str(row["average_price"])),
                reference_price=D(str(row.get("reference_price") or "0")),
                arrival_price=D(str(row.get("arrival_price") or "0")),
                created_time=int(row["created_time"]), submit_time=int(row.get("submit_time") or 0),
                ack_time=int(row.get("ack_time") or 0), first_fill_time=int(row.get("first_fill_time") or 0),
                final_fill_time=int(row.get("final_fill_time") or 0),
                slippage_bps=D(str(row.get("slippage_bps") or "0")),
                arrival_slippage_bps=D(str(row.get("arrival_slippage_bps") or "0")),
                updated_time=int(row["updated_time"]), error_code=str(row["error_code"]),
                error_message=str(row["error_message"]),
            )
            self.orders[order.client_order_id] = order
        for row in self.ledger.open_positions():
            metadata = json.loads(row.get("metadata_json") or "{}")
            position = LivePosition(
                position_id=str(row["position_id"]), intent_id=str(row["intent_id"]),
                symbol=str(row["symbol"]), status=str(row["status"]),
                quantity=D(str(row["quantity"])), entry_price=D(str(row["entry_price"])),
                structure_stop_price=D(str(row["structure_stop_price"])),
                trail_price=D(str(row["trail_price"])), liquidation_price=D(str(row["liquidation_price"])),
                entry_time=int(row["entry_time"]), exit_time=int(row["exit_time"]),
                exit_price=D(str(row["exit_price"])), realized_pnl=D(str(row["realized_pnl"])),
                entry_client_order_id=str(row["entry_client_order_id"]),
                structure_algo_id=row["structure_algo_id"], structure_client_algo_id=str(row["structure_client_algo_id"]),
                trail_algo_id=row["trail_algo_id"], trail_client_algo_id=str(row["trail_client_algo_id"]),
                protected=bool(row["protected"]), updated_time=int(row["updated_time"]), metadata=metadata,
            )
            self.positions[position.position_id] = position
            self.positions_by_symbol[position.symbol] = position

    def set_account(self, snapshot: AccountSnapshot, raw: dict[str, Any] | None = None) -> None:
        self.account = snapshot
        self.ledger.save_account_snapshot(snapshot, raw)
        self.refresh_sizing_state(initialize=self.orders_authorized)

    @property
    def sizing_start_time(self) -> int:
        return int(self.ledger.get_meta("sizing_start_time", "0") or 0)

    def refresh_sizing_state(self, *, initialize: bool = False) -> dict[str, Decimal]:
        """Refresh realized-equity sizing without treating cash transfers as PnL."""
        if self.account is None:
            return {}
        account_equity = max(D("0"), self.account.margin_balance)
        if self.config.sizing_mode != "realized_drawdown_ladder":
            self.risk.set_sizing_state(
                equity=account_equity,
                peak_equity=account_equity,
                drawdown_pct=D("0"),
                factor=D("1"),
            )
            return {
                "equity": account_equity,
                "peak_equity": account_equity,
                "drawdown_pct": D("0"),
                "factor": D("1"),
            }

        start_time = self.sizing_start_time
        initial_text = self.ledger.get_meta("sizing_initial_equity")
        if start_time <= 0 or not initial_text:
            current = account_equity
            peak = account_equity
            drawdown = D("0")
            factor = D(str(self.config.drawdown_factor(0.0)))
            if initialize:
                start_time = max(1, int(self.account.snapshot_time))
                stamp = now_ms()
                self.ledger.set_meta("sizing_start_time", str(start_time), stamp)
                self.ledger.set_meta("sizing_initial_equity", str(account_equity), stamp)
                self.ledger.set_meta("sizing_peak_equity", str(peak), stamp)
                self.ledger.append_event(
                    stamp,
                    "SIZING_BASELINE_INITIALIZED",
                    "",
                    {
                        "start_time": start_time,
                        "initial_equity": str(account_equity),
                        "sizing_mode": self.config.sizing_mode,
                    },
                )
        else:
            initial = max(D("0"), D(initial_text))
            trading_income = self.ledger.trading_income_since(start_time)
            current = max(D("0"), initial + trading_income)
            prior_peak = D(self.ledger.get_meta("sizing_peak_equity", str(initial)) or str(initial))
            peak = max(initial, prior_peak, current)
            drawdown = max(D("0"), D("1") - current / peak) if peak > 0 else D("0")
            factor = D(str(self.config.drawdown_factor(float(drawdown))))
            if initialize:
                stamp = now_ms()
                prior_factor = self.ledger.get_meta("sizing_factor")
                self.ledger.set_meta("sizing_peak_equity", str(peak), stamp)
                if prior_factor and D(prior_factor) != factor:
                    self.ledger.append_event(
                        stamp,
                        "SIZING_TIER_CHANGED",
                        "",
                        {
                            "equity": str(current),
                            "peak_equity": str(peak),
                            "drawdown_pct": str(drawdown),
                            "old_factor": prior_factor,
                            "new_factor": str(factor),
                        },
                    )

        self.risk.set_sizing_state(
            equity=current,
            peak_equity=peak,
            drawdown_pct=drawdown,
            factor=factor,
        )
        if initialize:
            stamp = now_ms()
            self.ledger.set_meta("sizing_current_equity", str(current), stamp)
            self.ledger.set_meta("sizing_current_drawdown", str(drawdown), stamp)
            self.ledger.set_meta("sizing_factor", str(factor), stamp)
        return {
            "equity": current,
            "peak_equity": peak,
            "drawdown_pct": drawdown,
            "factor": factor,
        }

    def safe_halt(self, reason: str) -> None:
        reason = str(reason or "").strip()
        if not reason:
            return
        existing = [item for item in self.safe_halt_reason.split(" | ") if item]
        if reason in existing:
            return
        existing.append(reason)
        self.safe_halt_reason = " | ".join(existing)
        self.ledger.set_meta("safe_halt_reason", self.safe_halt_reason, now_ms())
        self.ledger.append_event(
            now_ms(), "SAFE_HALT", "",
            {"reason": reason, "combined_reason": self.safe_halt_reason},
        )

    def clear_safe_halt_reasons(self, reasons: set[str]) -> list[str]:
        """Clear only faults proven recoverable by a successful reconcile.

        A private-stream reconnect must not erase an unrelated liquidation,
        position-mismatch, daily-loss, or unknown-order halt that was raised
        while the stream was unavailable.
        """
        wanted = {str(reason).strip() for reason in reasons if str(reason).strip()}
        existing = [item for item in self.safe_halt_reason.split(" | ") if item]
        cleared = [item for item in existing if item in wanted]
        if not cleared:
            return []
        remaining = [item for item in existing if item not in wanted]
        self.safe_halt_reason = " | ".join(remaining)
        stamp = now_ms()
        self.ledger.set_meta("safe_halt_reason", self.safe_halt_reason, stamp)
        self.ledger.append_event(
            stamp, "SAFE_HALT_RECOVERED", "",
            {"cleared": cleared, "remaining": remaining},
        )
        return cleared

    async def recover_inflight_orders(self) -> None:
        """Resolve persisted nonterminal/unapplied orders before accepting signals."""
        for order in list(self.orders.values()):
            try:
                result = await asyncio.to_thread(
                    self.gateway.rest.query_order,
                    order.symbol,
                    order_id=order.exchange_order_id,
                    client_order_id=None if order.exchange_order_id else order.client_order_id,
                )
            except GatewayError as exc:
                self.safe_halt(f"inflight_order_unresolved:{order.client_order_id}:{exc.code}")
                continue
            self._apply_order_result(order, result)
            if order.filled_quantity <= order.applied_quantity:
                continue
            stored_intent = self.ledger.intent(order.intent_id)
            if not stored_intent:
                self.safe_halt(f"inflight_intent_missing:{order.intent_id}")
                continue
            if order.side == "SELL" and not order.reduce_only:
                await self._apply_entry_fill(stored_intent, order, self.rules.get(order.symbol))
            elif order.side == "BUY" and order.reduce_only:
                position = self.positions.get(stored_intent.position_id) or self.positions_by_symbol.get(order.symbol)
                if not position:
                    self.safe_halt(f"inflight_exit_position_missing:{order.client_order_id}")
                    continue
                await self._apply_exit_fill(position, order)

    async def handle_intent(
        self,
        intent: TradeIntent,
        quote: BookQuote,
        depth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        inserted = self.ledger.save_intent(intent)
        self._intent_cache[intent.intent_id] = intent
        if not inserted:
            return {"status": "duplicate_intent", "intent_id": intent.intent_id}
        self.ledger.append_event(intent.decision_time, "INTENT_CREATED", intent.intent_id, intent.to_dict())
        if intent.action == IntentAction.OPEN_SHORT:
            return await self._open_short(intent, quote, depth)
        return await self._close_short(intent, quote)

    async def _open_short(
        self, intent: TradeIntent, quote: BookQuote, depth: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self.safe_halt_reason:
            return await self._record_rejected(intent, f"safe_halt:{self.safe_halt_reason}")
        if intent.symbol in self.positions_by_symbol:
            return await self._record_rejected(intent, "symbol_position_already_open")
        if self.account is None:
            return await self._record_rejected(intent, "account_snapshot_missing")
        symbol_rules = self.rules.get(intent.symbol)
        decision = self.risk.evaluate_short_entry(
            intent, quote, symbol_rules, self.account, len(self.positions_by_symbol), depth,
        )
        client_id = short_id("en", intent.intent_id)
        if not decision.approved:
            return await self._record_rejected(intent, decision.reason, client_id=client_id)
        plan = self.policy.open_short(intent, decision.quantity, quote, symbol_rules, client_id)
        order = LiveOrder(
            client_order_id=client_id, intent_id=intent.intent_id, symbol=intent.symbol,
            side="SELL", order_type=str(plan.initial_params["type"]), execution_policy=plan.policy,
            state=OrderState.RISK_APPROVED, quantity=decision.quantity,
            price=D(str(plan.initial_params.get("price") or "0")), reference_price=intent.signal_price,
            arrival_price=quote.bid_price,
            created_time=now_ms(), updated_time=now_ms(),
        )
        self.orders[client_id] = order
        self.ledger.save_order(order)
        self.ledger.append_event(now_ms(), "RISK_APPROVED", intent.intent_id, {
            "quantity": str(decision.quantity), "notional": str(decision.notional),
            "risk_usdt": str(decision.risk_usdt), "policy": plan.policy,
            "maker_wait_ms": plan.maker_wait_ms,
            "sizing_mode": self.config.sizing_mode,
            "sizing_equity": str(decision.sizing_equity),
            "margin_fraction": str(decision.margin_fraction),
            "drawdown_pct": str(decision.drawdown_pct),
            "sizing_factor": str(decision.sizing_factor),
        })
        if not self.can_send_orders:
            return {"status": "dry_run", "policy": plan.policy, "order": order.to_dict()}
        try:
            await self._configure_symbol(intent.symbol)
        except (GatewayError, UnknownExecutionStatus) as exc:
            order.state = OrderState.EXCHANGE_REJECTED
            order.error_code = str(exc.code or "")
            order.error_message = f"symbol_configuration_failed:{exc}"
            order.updated_time = now_ms()
            self.ledger.save_order(order)
            self.safe_halt(f"symbol_configuration_failed:{intent.symbol}:{type(exc).__name__}")
            return {
                "status": "configuration_failed",
                "reason": str(exc),
                "order": order.to_dict(),
            }
        return await self._execute_open_plan(intent, order, plan, symbol_rules)

    async def _record_rejected(
        self, intent: TradeIntent, reason: str, client_id: str | None = None
    ) -> dict[str, Any]:
        order = LiveOrder(
            client_order_id=client_id or short_id("rj", intent.intent_id), intent_id=intent.intent_id,
            symbol=intent.symbol, side="SELL" if intent.action == IntentAction.OPEN_SHORT else "BUY",
            order_type="NONE", execution_policy="none", state=OrderState.RISK_REJECTED,
            quantity=D("0"), reference_price=intent.signal_price,
            created_time=now_ms(), updated_time=now_ms(), error_message=reason,
        )
        self.orders[order.client_order_id] = order
        self.ledger.save_order(order)
        self.ledger.append_event(now_ms(), "RISK_REJECTED", intent.intent_id, {"reason": reason})
        return {"status": "rejected", "reason": reason}

    async def _configure_symbol(self, symbol: str) -> None:
        if symbol in self._configured_symbols:
            return
        await asyncio.to_thread(self.gateway.rest.set_leverage, symbol, self.config.leverage)
        if self.config.isolated_margin and self.config.account_api != "portfolio_margin":
            try:
                await asyncio.to_thread(self.gateway.rest.set_margin_type, symbol, "ISOLATED")
            except GatewayError as exc:
                # -4046 means the requested margin type is already active.
                if str(exc.code) != "-4046" and "No need to change" not in str(exc):
                    raise
        self._configured_symbols.add(symbol)

    async def _execute_open_plan(
        self, intent: TradeIntent, order: LiveOrder, plan: OrderPlan, rules: SymbolRules
    ) -> dict[str, Any]:
        order.state = OrderState.SUBMITTING
        order.submit_time = order.submit_time or self._exchange_now_ms()
        order.updated_time = order.submit_time
        self.ledger.save_order(order)
        try:
            result = await self.gateway.trade_ws.place_order(plan.initial_params)
        except UnknownExecutionStatus as exc:
            return await self._resolve_unknown_order(intent, order, rules, exc)
        except GatewayError as exc:
            order.state = OrderState.EXCHANGE_REJECTED
            order.error_code = str(exc.code or "")
            order.error_message = str(exc)
            order.updated_time = now_ms()
            self.ledger.save_order(order)
            return {"status": "exchange_rejected", "reason": str(exc)}
        self._apply_order_result(order, result)
        if order.order_type == "MARKET" and not await self._settle_market_order(order):
            await self._abort_unsettled_entry(order, "market_entry_not_settled")
            return {
                "status": "unknown",
                "policy": plan.policy,
                "order": order.to_dict(),
                "position": None,
            }
        if order.filled_quantity > order.applied_quantity:
            await self._apply_entry_fill(intent, order, rules)
        if plan.policy == "maker_first" and order.state not in {OrderState.FILLED, OrderState.CLOSED}:
            await asyncio.sleep(plan.maker_wait_ms / 1000.0)
            await self._refresh_order(order)
            if order.filled_quantity > order.applied_quantity:
                await self._apply_entry_fill(intent, order, rules)
            if order.state != OrderState.FILLED:
                cancel_confirmed = await self._cancel_if_open(order)
                if order.filled_quantity > order.applied_quantity:
                    await self._apply_entry_fill(intent, order, rules)
                if not cancel_confirmed:
                    return {
                        "status": "cancel_unknown",
                        "policy": plan.policy,
                        "order": order.to_dict(),
                        "position": self.positions.get(intent.position_id).to_dict()
                        if self.positions.get(intent.position_id) else None,
                    }
                remaining = max(D("0"), order.quantity - order.filled_quantity)
                if remaining > 0 and plan.fallback_params and self.can_send_orders:
                    params = dict(plan.fallback_params)
                    params["quantity"] = decimal_text(remaining)
                    fallback_id = str(params["newClientOrderId"])
                    fallback = LiveOrder(
                        client_order_id=fallback_id, intent_id=intent.intent_id, symbol=intent.symbol,
                        side="SELL", order_type="MARKET", execution_policy="maker_fallback_market",
                        state=OrderState.SUBMITTING, quantity=remaining, reference_price=intent.signal_price,
                        arrival_price=order.arrival_price,
                        created_time=now_ms(), submit_time=self._exchange_now_ms(), updated_time=now_ms(),
                    )
                    self.orders[fallback_id] = fallback
                    self.ledger.save_order(fallback)
                    try:
                        fallback_result = await self.gateway.trade_ws.place_order(params)
                        self._apply_order_result(fallback, fallback_result)
                        if not await self._settle_market_order(fallback):
                            await self._abort_unsettled_entry(fallback, "fallback_market_not_settled")
                            return {
                                "status": "unknown",
                                "policy": plan.policy,
                                "order": fallback.to_dict(),
                                "position": self.positions.get(intent.position_id).to_dict()
                                if self.positions.get(intent.position_id) else None,
                            }
                        if fallback.filled_quantity > fallback.applied_quantity:
                            await self._apply_entry_fill(intent, fallback, rules)
                    except UnknownExecutionStatus as exc:
                        await self._resolve_unknown_order(intent, fallback, rules, exc)
                    except GatewayError as exc:
                        fallback.state = OrderState.EXCHANGE_REJECTED
                        fallback.error_code = str(exc.code or "")
                        fallback.error_message = str(exc)
                        fallback.updated_time = now_ms()
                        self.ledger.save_order(fallback)
                        self.safe_halt(f"fallback_entry_rejected:{fallback.client_order_id}")
        position = self.positions.get(intent.position_id)
        return {
            "status": "filled" if position else order.state.value.lower(),
            "policy": plan.policy,
            "order": order.to_dict(),
            "position": position.to_dict() if position else None,
        }

    def _apply_order_result(self, order: LiveOrder, result: dict[str, Any]) -> None:
        received_time = self._exchange_now_ms()
        exchange_update_time = int(
            result.get("updateTime") or result.get("time") or result.get("T") or received_time
        )
        order.exchange_order_id = int(result.get("orderId") or order.exchange_order_id or 0) or None
        order.filled_quantity = D(str(result.get("executedQty") or order.filled_quantity or "0"))
        order.average_price = D(str(result.get("avgPrice") or order.average_price or "0"))
        if order.exchange_order_id is not None and order.ack_time <= 0:
            order.ack_time = received_time
        if order.filled_quantity > 0 and order.first_fill_time <= 0:
            order.first_fill_time = exchange_update_time
        status = str(result.get("status") or "NEW").upper()
        if status == "FILLED":
            order.state = OrderState.FILLED
            order.final_fill_time = order.final_fill_time or exchange_update_time
        elif status == "PARTIALLY_FILLED":
            order.state = OrderState.PARTIALLY_FILLED
        elif status in {"CANCELED", "CANCELLED"}:
            order.state = OrderState.CANCELLED
        elif status == "EXPIRED":
            order.state = OrderState.EXPIRED
        elif status == "REJECTED":
            order.state = OrderState.EXCHANGE_REJECTED
        else:
            order.state = OrderState.ACKED
        self._update_slippage(order)
        order.updated_time = max(received_time, exchange_update_time)
        self.ledger.save_order(order)
        self.ledger.append_event(order.updated_time, "ORDER_UPDATE", order.client_order_id, {
            "status": status, "order_id": order.exchange_order_id,
            "filled_quantity": str(order.filled_quantity), "average_price": str(order.average_price),
        })
        waiter = self._event_waiters.get(order.client_order_id)
        if waiter:
            waiter.set()

    @staticmethod
    def _update_slippage(order: LiveOrder) -> None:
        if order.average_price <= 0:
            return
        direction = D("1") if order.side.upper() == "BUY" else D("-1")
        if order.reference_price > 0:
            order.slippage_bps = (
                direction * (order.average_price - order.reference_price)
                / order.reference_price * D("10000")
            )
        if order.arrival_price > 0:
            order.arrival_slippage_bps = (
                direction * (order.average_price - order.arrival_price)
                / order.arrival_price * D("10000")
            )

    async def _refresh_order(self, order: LiveOrder) -> bool:
        try:
            result = await asyncio.to_thread(
                self.gateway.rest.query_order,
                order.symbol,
                order_id=order.exchange_order_id,
                client_order_id=None if order.exchange_order_id else order.client_order_id,
            )
            self._apply_order_result(order, result)
            return True
        except GatewayError as exc:
            self.ledger.append_event(now_ms(), "ORDER_QUERY_FAILED", order.client_order_id, {"error": str(exc)})
            return False

    async def _settle_market_order(self, order: LiveOrder) -> bool:
        """Resolve asynchronous Portfolio Margin MARKET acknowledgements."""
        for delay in (0.0, 0.05, 0.10, 0.20, 0.40):
            if order.filled_quantity > 0:
                return await self._ensure_execution_price(order)
            if order.state in {
                OrderState.CANCELLED, OrderState.EXPIRED, OrderState.EXCHANGE_REJECTED,
            }:
                return True
            if delay:
                await asyncio.sleep(delay)
            await self._refresh_order(order)
        self.ledger.append_event(
            now_ms(), "MARKET_ORDER_UNSETTLED", order.client_order_id,
            {"symbol": order.symbol, "state": order.state.value},
        )
        return False

    async def _ensure_execution_price(self, order: LiveOrder) -> bool:
        if order.filled_quantity <= order.applied_quantity:
            return True
        if order.average_price > 0:
            return True
        await self._refresh_order(order)
        if order.average_price > 0:
            return True
        start_time = max(order.created_time - 60_000, now_ms() - 7 * 86_400_000)
        try:
            rows = await asyncio.to_thread(
                self.gateway.rest.user_trades, order.symbol, start_time, 1000,
            )
        except GatewayError as exc:
            self.ledger.append_event(
                now_ms(), "ORDER_TRADES_QUERY_FAILED", order.client_order_id, {"error": str(exc)},
            )
            return False
        matches = [
            row for row in rows
            if order.exchange_order_id is not None
            and int(row.get("orderId") or -1) == int(order.exchange_order_id)
        ]
        quantity = sum((D(str(row.get("qty") or "0")) for row in matches), D("0"))
        notional = sum(
            (
                D(str(row.get("qty") or "0")) * D(str(row.get("price") or "0"))
                for row in matches
            ),
            D("0"),
        )
        if quantity <= 0 or notional <= 0:
            return False
        order.filled_quantity = max(order.filled_quantity, quantity)
        order.average_price = notional / quantity
        trade_times = sorted(int(row.get("time") or 0) for row in matches if int(row.get("time") or 0) > 0)
        if trade_times:
            order.first_fill_time = order.first_fill_time or trade_times[0]
            if order.state == OrderState.FILLED:
                order.final_fill_time = order.final_fill_time or trade_times[-1]
        self._update_slippage(order)
        order.updated_time = now_ms()
        self.ledger.save_order(order)
        for row in matches:
            trade_id = int(row.get("id") or row.get("tradeId") or -1)
            if trade_id < 0:
                continue
            self.ledger.save_fill(LiveFill(
                exchange_order_id=int(row.get("orderId") or 0), trade_id=trade_id,
                client_order_id=order.client_order_id, symbol=order.symbol,
                side=str(row.get("side") or order.side),
                quantity=D(str(row.get("qty") or "0")), price=D(str(row.get("price") or "0")),
                commission=D(str(row.get("commission") or "0")),
                commission_asset=str(row.get("commissionAsset") or ""),
                realized_pnl=D(str(row.get("realizedPnl") or "0")),
                maker=bool(row.get("maker")), trade_time=int(row.get("time") or now_ms()),
            ))
        self.ledger.append_event(
            order.updated_time, "ORDER_PRICE_RECOVERED", order.client_order_id,
            {"source": "user_trades", "quantity": str(quantity), "average_price": str(order.average_price)},
        )
        return True

    async def _exchange_short_quantity(self, symbol: str) -> Decimal:
        rows = await asyncio.to_thread(self.gateway.rest.position_risk, symbol)
        row = next((
            item for item in rows
            if str(item.get("symbol") or "") == symbol
            and str(item.get("positionSide") or self.config.position_side) == self.config.position_side
        ), None)
        return abs(D(str((row or {}).get("positionAmt") or "0")))

    async def _abort_unsettled_entry(self, order: LiveOrder, reason: str) -> None:
        self.safe_halt(f"{reason}:{order.client_order_id}")
        if order.state not in {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED}:
            await self._cancel_if_open(order)
        await asyncio.sleep(0.10)
        try:
            quantity = await self._exchange_short_quantity(order.symbol)
        except GatewayError as exc:
            self.ledger.append_event(
                now_ms(), "UNTRACKED_POSITION_QUERY_FAILED", order.client_order_id, {"error": str(exc)},
            )
            return
        if quantity <= 0:
            return
        client_id = short_id("uf", f"{order.client_order_id}:{now_ms()}")
        params = {
            "symbol": order.symbol, "side": "BUY", "positionSide": self.config.position_side,
            "type": "MARKET", "quantity": decimal_text(quantity),
            "newClientOrderId": client_id, "newOrderRespType": "RESULT",
        }
        if self.config.exchange_reduce_only:
            params["reduceOnly"] = "true"
        flatten = LiveOrder(
            client_order_id=client_id, intent_id=order.intent_id, symbol=order.symbol,
            side="BUY", order_type="MARKET", execution_policy="untracked_flatten",
            state=OrderState.SUBMITTING, quantity=quantity, reduce_only=True,
            reference_price=order.average_price or order.reference_price,
            created_time=now_ms(), submit_time=self._exchange_now_ms(), updated_time=now_ms(),
        )
        self.orders[client_id] = flatten
        self.ledger.save_order(flatten)
        try:
            result = await self.gateway.trade_ws.place_order(params)
            self._apply_order_result(flatten, result)
            await self._settle_market_order(flatten)
        except (GatewayError, UnknownExecutionStatus) as exc:
            self.ledger.append_event(
                now_ms(), "UNTRACKED_FLATTEN_FAILED", client_id, {"error": str(exc)},
            )
        remaining = await self._exchange_short_quantity(order.symbol)
        if remaining <= 0:
            flatten.applied_quantity = flatten.filled_quantity
            flatten.applied_notional = flatten.filled_quantity * flatten.average_price
            flatten.state = OrderState.CLOSED
            flatten.updated_time = now_ms()
            self.ledger.save_order(flatten)
            order.applied_quantity = order.filled_quantity
            order.applied_notional = order.filled_quantity * order.average_price
            order.state = OrderState.CLOSED
            order.updated_time = now_ms()
            self.ledger.save_order(order)
        self.ledger.append_event(
            now_ms(), "UNTRACKED_POSITION_FLATTENED", order.client_order_id,
            {"requested_quantity": str(quantity), "remaining_quantity": str(remaining)},
        )

    async def _resolve_unknown_order(
        self, intent: TradeIntent, order: LiveOrder, rules: SymbolRules, exc: Exception
    ) -> dict[str, Any]:
        order.state = OrderState.UNKNOWN
        order.error_message = str(exc)
        order.updated_time = now_ms()
        self.ledger.save_order(order)
        await asyncio.sleep(0.25)
        try:
            result = await asyncio.to_thread(
                self.gateway.rest.query_order, order.symbol, client_order_id=order.client_order_id,
            )
        except GatewayError:
            await self._abort_unsettled_entry(order, "unknown_order_unresolved")
            return {"status": "unknown", "client_order_id": order.client_order_id}
        self._apply_order_result(order, result)
        if order.order_type == "MARKET" and not await self._settle_market_order(order):
            await self._abort_unsettled_entry(order, "unknown_market_order_unsettled")
            return {"status": "unknown", "client_order_id": order.client_order_id}
        if order.filled_quantity > order.applied_quantity:
            await self._apply_entry_fill(intent, order, rules)
        if order.state not in {
            OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED,
            OrderState.EXCHANGE_REJECTED,
        }:
            self.safe_halt(f"pending_unknown_order:{order.client_order_id}")
        return {"status": order.state.value.lower(), "order": order.to_dict()}

    async def _cancel_if_open(self, order: LiveOrder) -> bool:
        if order.state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED}:
            return True
        try:
            result = await self.gateway.trade_ws.cancel_order(order.symbol, order.client_order_id)
            self._apply_order_result(order, result)
        except UnknownExecutionStatus:
            if not await self._refresh_order(order):
                self.safe_halt(f"cancel_status_unknown:{order.client_order_id}")
                return False
        except GatewayError as exc:
            # If it filled while canceling, the following query recovers the final status.
            self.ledger.append_event(now_ms(), "CANCEL_FAILED", order.client_order_id, {"error": str(exc)})
            if not await self._refresh_order(order):
                self.safe_halt(f"cancel_status_unknown:{order.client_order_id}")
                return False
        terminal = order.state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED}
        if not terminal:
            self.safe_halt(f"cancel_not_terminal:{order.client_order_id}:{order.state.value}")
        return terminal

    async def _apply_entry_fill(
        self, intent: TradeIntent, order: LiveOrder, rules: SymbolRules
    ) -> LivePosition:
        if order.filled_quantity <= 0 or not await self._ensure_execution_price(order):
            await self._abort_unsettled_entry(order, "entry_fill_price_unresolved")
            raise RuntimeError("cannot create position without a confirmed positive fill price")
        delta = order.filled_quantity - order.applied_quantity
        if delta <= 0:
            position = self.positions.get(intent.position_id)
            if not position:
                raise RuntimeError("applied entry fill has no position")
            return position
        cumulative_notional = order.filled_quantity * order.average_price
        delta_notional = cumulative_notional - order.applied_notional
        if delta_notional <= 0:
            delta_notional = delta * order.average_price
        delta_price = delta_notional / delta
        position = self.positions.get(intent.position_id)
        replace_structure = False
        if position:
            old_notional = position.entry_price * position.quantity
            old_stop = position.structure_stop_price
            new_notional = delta_notional
            position.quantity += delta
            position.entry_price = (old_notional + new_notional) / position.quantity
            position.structure_stop_price = rules.price_up(
                max(intent.strategy_stop_price, position.entry_price * D("1.015"))
            )
            # Every added fill changes the protected quantity, even when the
            # stop price is unchanged. The exchange algo must cover the full
            # authoritative position quantity.
            replace_structure = position.protected
            position.updated_time = now_ms()
        else:
            stop = rules.price_up(max(intent.strategy_stop_price, order.average_price * D("1.015")))
            position = LivePosition(
                position_id=intent.position_id, intent_id=intent.intent_id, symbol=intent.symbol,
                status="open", quantity=delta, entry_price=delta_price,
                structure_stop_price=stop, entry_time=now_ms(), updated_time=now_ms(),
                entry_client_order_id=order.client_order_id,
                metadata={
                    "signal_id": intent.signal_id,
                    "strategy": intent.strategy,
                    "sizing_mode": self.config.sizing_mode,
                    "sizing_equity": str(self.risk.sizing_equity),
                    "drawdown_at_entry": str(self.risk.sizing_drawdown_pct),
                    "sizing_factor": str(self.risk.sizing_factor),
                    "margin_fraction": str(
                        min(
                            D(str(self.config.margin_fraction_cap)),
                            D(str(self.config.base_margin_fraction)) * self.risk.sizing_factor,
                        )
                    ),
                },
            )
            self.positions[position.position_id] = position
            self.positions_by_symbol[position.symbol] = position
        order.applied_quantity = order.filled_quantity
        order.applied_notional = cumulative_notional
        position.metadata["initial_quantity"] = str(
            D(str(position.metadata.get("initial_quantity") or "0")) + delta
        )
        self.ledger.save_order(order)
        self.ledger.save_position(position)
        if self.safe_halt_reason:
            await self.emergency_close(position, f"entry_fill_during_safe_halt:{self.safe_halt_reason}")
            return position
        await self.ensure_structure_protection(position, rules, force_replace=replace_structure)
        actual_slippage = max(order.slippage_bps, order.arrival_slippage_bps)
        if actual_slippage > D(str(self.config.max_entry_slippage_bps)):
            self.ledger.append_event(
                now_ms(),
                "ENTRY_SLIPPAGE_LIMIT_EXCEEDED",
                order.client_order_id,
                {
                    "signal_slippage_bps": str(order.slippage_bps),
                    "arrival_slippage_bps": str(order.arrival_slippage_bps),
                    "limit_bps": str(self.config.max_entry_slippage_bps),
                },
            )
            await self.emergency_close(
                position,
                f"entry_slippage_exceeded:{actual_slippage}bps",
            )
            return position
        await self._refresh_liquidation(position)
        return position

    async def ensure_structure_protection(
        self, position: LivePosition, rules: SymbolRules, *, force_replace: bool = False
    ) -> None:
        if position.protected and position.structure_client_algo_id and not force_replace:
            return
        old_client_id = position.structure_client_algo_id
        old_algo_id = position.structure_algo_id
        old_protected = position.protected
        old_stop = D(str(position.metadata.get("active_structure_stop_price") or position.structure_stop_price))
        client_algo_id = short_id(
            "sl", f"{position.position_id}:{position.structure_stop_price}:{position.quantity}"
        )
        position.structure_client_algo_id = client_algo_id
        position.protected = False
        position.updated_time = now_ms()
        self.ledger.save_position(position)
        params = {
            "algoType": "CONDITIONAL", "symbol": position.symbol, "side": "BUY",
            "positionSide": self.config.position_side, "type": "STOP_MARKET",
            "quantity": decimal_text(position.quantity),
            "triggerPrice": decimal_text(rules.price_up(position.structure_stop_price)),
            "workingType": "CONTRACT_PRICE", "priceProtect": "false",
            "clientAlgoId": client_algo_id, "newOrderRespType": "ACK",
        }
        if self.config.exchange_reduce_only:
            params["reduceOnly"] = "true"
        self.ledger.save_algo({
            "client_algo_id": client_algo_id, "position_id": position.position_id,
            "symbol": position.symbol, "role": "structure_stop", "status": "SUBMITTING",
            "trigger_price": position.structure_stop_price, "updated_time": now_ms(), "raw": {},
        })
        try:
            result = await self._place_algo_confirmed(params)
            position.structure_algo_id = int(result.get("algoId") or 0) or None
            position.structure_client_algo_id = client_algo_id
            status = str(result.get("algoStatus") or "NEW").upper()
            position.protected = status in {"NEW", "ACCEPTED", "PENDING", "WORKING"}
            position.metadata["active_structure_stop_price"] = str(position.structure_stop_price)
            position.updated_time = now_ms()
            self.ledger.save_algo({
                "client_algo_id": client_algo_id, "position_id": position.position_id,
                "symbol": position.symbol, "role": "structure_stop", "algo_id": position.structure_algo_id,
                "status": status, "trigger_price": position.structure_stop_price,
                "updated_time": position.updated_time, "raw": result,
            })
            self.ledger.save_position(position)
            if not position.protected:
                raise GatewayError(f"structure protection not active: {status}")
            if old_client_id and old_client_id != client_algo_id:
                try:
                    await self._cancel_algo_confirmed(old_client_id)
                except (GatewayError, UnknownExecutionStatus) as exc:
                    self.ledger.append_event(
                        now_ms(), "OLD_STRUCTURE_CANCEL_FAILED", old_client_id, {"error": str(exc)},
                    )
                    self.safe_halt(f"old_structure_cancel_unresolved:{old_client_id}")
        except Exception as exc:
            self.ledger.append_event(now_ms(), "PROTECTION_FAILED", position.position_id, {"error": str(exc)})
            if old_client_id and old_protected:
                position.structure_client_algo_id = old_client_id
                position.structure_algo_id = old_algo_id
                position.structure_stop_price = old_stop
                position.protected = True
                position.updated_time = now_ms()
                self.ledger.save_position(position)
                if force_replace:
                    await self.emergency_close(
                        position, f"protection_resize_failed:{type(exc).__name__}",
                    )
                return
            await self.emergency_close(position, f"protection_failed:{type(exc).__name__}")

    async def _place_algo_confirmed(self, params: dict[str, Any]) -> dict[str, Any]:
        """Place an algo order and resolve an ACK timeout by client id."""
        client_algo_id = str(params["clientAlgoId"])
        try:
            return await asyncio.wait_for(
                self.gateway.trade_ws.place_algo(params),
                timeout=max(0.25, self.config.protection_timeout_ms / 1000.0),
            )
        except (asyncio.TimeoutError, UnknownExecutionStatus) as original:
            last_error: Exception = original
            for delay in (0.05, 0.15, 0.30):
                await asyncio.sleep(delay)
                try:
                    return await asyncio.to_thread(
                        self.gateway.rest.query_algo_order,
                        client_algo_id=client_algo_id,
                    )
                except GatewayError as exc:
                    last_error = exc
            raise UnknownExecutionStatus(
                f"algo status unresolved for {client_algo_id}: {type(last_error).__name__}"
            ) from original

    async def _cancel_algo_confirmed(self, client_algo_id: str) -> dict[str, Any]:
        terminal_states = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FINISHED"}
        original: Exception | None = None
        try:
            result = await self.gateway.trade_ws.cancel_algo(client_algo_id)
            status = str(result.get("algoStatus") or result.get("status") or "").upper()
            if status in terminal_states:
                return result
            original = UnknownExecutionStatus(
                f"algo cancel response not terminal for {client_algo_id}: {status or 'UNKNOWN'}"
            )
        except (GatewayError, UnknownExecutionStatus) as exc:
            original = exc
        try:
            result = await asyncio.to_thread(
                self.gateway.rest.query_algo_order, client_algo_id=client_algo_id,
            )
        except GatewayError as query_error:
            raise UnknownExecutionStatus(
                f"algo cancel status unresolved for {client_algo_id}: {type(query_error).__name__}"
            ) from original
        status = str(result.get("algoStatus") or result.get("status") or "").upper()
        if status not in terminal_states:
            raise UnknownExecutionStatus(
                f"algo cancel not terminal for {client_algo_id}: {status or 'UNKNOWN'}"
            ) from original
        return result

    async def _refresh_liquidation(self, position: LivePosition) -> None:
        try:
            rows = await asyncio.to_thread(self.gateway.rest.position_risk, position.symbol)
            row = next((
                x for x in rows
                if str(x.get("symbol")) == position.symbol
                and str(x.get("positionSide") or self.config.position_side) == self.config.position_side
            ), None)
            if row:
                position.liquidation_price = D(str(row.get("liquidationPrice") or "0"))
                self.ledger.save_position(position)
                if not self.risk.actual_liquidation_is_safe(
                    position.entry_price, position.structure_stop_price, position.liquidation_price,
                ):
                    await self.emergency_close(position, "liquidation_too_close")
        except GatewayError as exc:
            self.ledger.append_event(now_ms(), "LIQUIDATION_QUERY_FAILED", position.position_id, {"error": str(exc)})

    async def _close_short(self, intent: TradeIntent, quote: BookQuote) -> dict[str, Any]:
        position = self.positions.get(intent.position_id) or self.positions_by_symbol.get(intent.symbol)
        if not position:
            self.ledger.append_event(now_ms(), "EXIT_WITHOUT_LIVE_POSITION", intent.intent_id, {"symbol": intent.symbol})
            return {"status": "no_live_position"}
        if position.status == "closing":
            return {"status": "already_closing", "position_id": position.position_id}
        rules = self.rules.get(position.symbol)
        urgent = intent.reason.startswith("stop") or "emergency" in intent.reason
        client_id = short_id("ex", intent.intent_id)
        plan = self.policy.close_short(intent, position.quantity, quote, rules, client_id, urgent)
        order = LiveOrder(
            client_order_id=client_id, intent_id=intent.intent_id, symbol=intent.symbol,
            side="BUY", order_type=str(plan.initial_params["type"]), execution_policy=plan.policy,
            state=OrderState.RISK_APPROVED, quantity=position.quantity,
            price=D(str(plan.initial_params.get("price") or "0")), reduce_only=True,
            reference_price=intent.signal_price, arrival_price=quote.ask_price,
            created_time=now_ms(), updated_time=now_ms(),
        )
        self.orders[client_id] = order
        self.ledger.save_order(order)
        if not self.can_send_reduce_orders:
            return {"status": "dry_run_exit", "order": order.to_dict()}
        position.status = "closing"
        position.updated_time = now_ms()
        self.ledger.save_position(position)
        try:
            order.state = OrderState.SUBMITTING
            order.submit_time = order.submit_time or self._exchange_now_ms()
            order.updated_time = order.submit_time
            self.ledger.save_order(order)
            result = await self.gateway.trade_ws.place_order(plan.initial_params)
            self._apply_order_result(order, result)
            if order.order_type == "MARKET" and not await self._settle_market_order(order):
                self.safe_halt(f"market_exit_not_settled:{order.client_order_id}")
                position.status = "open"
                self.ledger.save_position(position)
                return {"status": "unknown", "order": order.to_dict()}
            if order.filled_quantity > order.applied_quantity:
                await self._apply_exit_fill(position, order)
            if position.quantity > 0 and plan.policy in {"maker_first", "ioc"}:
                if plan.policy == "maker_first":
                    await asyncio.sleep(plan.maker_wait_ms / 1000.0)
                    await self._refresh_order(order)
                if order.filled_quantity > order.applied_quantity:
                    await self._apply_exit_fill(position, order)
                if order.state != OrderState.FILLED:
                    cancel_confirmed = await self._cancel_if_open(order)
                    if not cancel_confirmed:
                        return {"status": "cancel_unknown", "order": order.to_dict()}
                if order.filled_quantity > order.applied_quantity:
                    await self._apply_exit_fill(position, order)
                remaining = max(D("0"), position.quantity)
                if remaining > 0 and plan.fallback_params:
                    params = dict(plan.fallback_params)
                    params["quantity"] = decimal_text(remaining)
                    fallback = LiveOrder(
                        client_order_id=str(params["newClientOrderId"]), intent_id=intent.intent_id,
                        symbol=intent.symbol, side="BUY", order_type="MARKET",
                        execution_policy=f"{plan.policy}_fallback_market", state=OrderState.SUBMITTING,
                        quantity=remaining, reduce_only=True, reference_price=intent.signal_price,
                        arrival_price=order.arrival_price,
                        created_time=now_ms(), submit_time=self._exchange_now_ms(), updated_time=now_ms(),
                    )
                    self.orders[fallback.client_order_id] = fallback
                    self.ledger.save_order(fallback)
                    try:
                        fallback_result = await self.gateway.trade_ws.place_order(params)
                        self._apply_order_result(fallback, fallback_result)
                        if not await self._settle_market_order(fallback):
                            self.safe_halt(f"fallback_exit_not_settled:{fallback.client_order_id}")
                            return {"status": "unknown", "order": fallback.to_dict()}
                        if fallback.filled_quantity > fallback.applied_quantity:
                            await self._apply_exit_fill(position, fallback)
                    except UnknownExecutionStatus as exc:
                        await self._resolve_unknown_exit_order(position, fallback, exc)
                    except GatewayError as exc:
                        fallback.state = OrderState.EXCHANGE_REJECTED
                        fallback.error_code = str(exc.code or "")
                        fallback.error_message = str(exc)
                        fallback.updated_time = now_ms()
                        self.ledger.save_order(fallback)
                        if position.quantity > 0:
                            position.status = "open"
                            position.updated_time = fallback.updated_time
                            self.ledger.save_position(position)
                        self.safe_halt(f"fallback_exit_rejected:{fallback.client_order_id}")
        except UnknownExecutionStatus as exc:
            await self._resolve_unknown_exit_order(position, order, exc)
        except GatewayError as exc:
            order.state = OrderState.EXCHANGE_REJECTED
            order.error_code = str(exc.code or "")
            order.error_message = str(exc)
            order.updated_time = now_ms()
            self.ledger.save_order(order)
            if position.quantity > 0:
                position.status = "open"
                position.updated_time = order.updated_time
                self.ledger.save_position(position)
            self.safe_halt(f"exit_order_rejected:{order.client_order_id}")
        return {"status": position.status, "order": order.to_dict()}

    async def _resolve_unknown_exit_order(
        self, position: LivePosition, order: LiveOrder, exc: Exception
    ) -> bool:
        order.state = OrderState.UNKNOWN
        order.error_message = str(exc)
        order.updated_time = now_ms()
        self.ledger.save_order(order)
        await asyncio.sleep(0.25)
        try:
            result = await asyncio.to_thread(
                self.gateway.rest.query_order,
                order.symbol,
                client_order_id=order.client_order_id,
            )
        except GatewayError:
            self.safe_halt(f"unknown_exit_order:{order.client_order_id}")
            return False
        self._apply_order_result(order, result)
        if order.filled_quantity > order.applied_quantity:
            await self._apply_exit_fill(position, order)
        if order.state in {OrderState.CANCELLED, OrderState.EXPIRED, OrderState.EXCHANGE_REJECTED}:
            if position.quantity > 0:
                position.status = "open"
                position.updated_time = now_ms()
                self.ledger.save_position(position)
            return True
        if order.state != OrderState.FILLED:
            self.safe_halt(f"pending_unknown_exit_order:{order.client_order_id}")
            return False
        return True

    async def emergency_close(self, position: LivePosition, reason: str) -> None:
        if not self.config.sends_real_orders or not self.orders_authorized:
            self.safe_halt(f"unprotected_dry_position:{position.symbol}:{reason}")
            return
        client_id = short_id("em", f"{position.position_id}:{reason}:{now_ms()}")
        params = {
            "symbol": position.symbol, "side": "BUY", "positionSide": self.config.position_side,
            "type": "MARKET", "quantity": decimal_text(position.quantity),
            "newClientOrderId": client_id, "newOrderRespType": "RESULT",
        }
        if self.config.exchange_reduce_only:
            params["reduceOnly"] = "true"
        order = LiveOrder(
            client_order_id=client_id, intent_id=position.intent_id, symbol=position.symbol,
            side="BUY", order_type="MARKET", execution_policy="emergency_market",
            state=OrderState.SUBMITTING, quantity=position.quantity, reduce_only=True,
            reference_price=position.entry_price,
            created_time=now_ms(), submit_time=self._exchange_now_ms(), updated_time=now_ms(),
        )
        self.orders[client_id] = order
        self.ledger.save_order(order)
        try:
            result = await self.gateway.trade_ws.place_order(params)
            self._apply_order_result(order, result)
            await self._settle_market_order(order)
            if order.filled_quantity > order.applied_quantity:
                await self._apply_exit_fill(position, order)
        except UnknownExecutionStatus as exc:
            await self._resolve_unknown_exit_order(position, order, exc)
        except GatewayError as exc:
            order.state = OrderState.EXCHANGE_REJECTED
            order.error_code = str(exc.code or "")
            order.error_message = str(exc)
            order.updated_time = now_ms()
            self.ledger.save_order(order)
            if position.quantity > 0:
                position.status = "open"
                position.updated_time = order.updated_time
                self.ledger.save_position(position)
        finally:
            self.safe_halt(f"emergency_close:{position.symbol}:{reason}")

    async def _apply_exit_fill(self, position: LivePosition, order: LiveOrder) -> None:
        if order.filled_quantity > order.applied_quantity and not await self._ensure_execution_price(order):
            self.safe_halt(f"exit_fill_price_unresolved:{order.client_order_id}")
            return
        delta = min(position.quantity, order.filled_quantity - order.applied_quantity)
        if delta <= 0:
            return
        cumulative_notional = order.filled_quantity * order.average_price
        delta_notional = cumulative_notional - order.applied_notional
        if delta_notional <= 0:
            delta_notional = delta * order.average_price
        delta_price = delta_notional / delta
        remaining = position.quantity - delta
        order.applied_quantity += delta
        order.applied_notional += delta_notional
        self.ledger.save_order(order)
        position.realized_pnl += (position.entry_price - delta_price) * delta
        closed_quantity = D(str(position.metadata.get("closed_quantity") or "0")) + delta
        exit_notional = D(str(position.metadata.get("exit_notional") or "0")) + delta_notional
        position.metadata["closed_quantity"] = str(closed_quantity)
        position.metadata["exit_notional"] = str(exit_notional)
        position.quantity = remaining
        position.updated_time = now_ms()
        if remaining > 0:
            position.status = "open"
            self.ledger.save_position(position)
            # A partial close changes the quantity protected by both algos.
            # Replace new-before-old so the residual short is never naked.
            await self.ensure_structure_protection(
                position, self.rules.get(position.symbol), force_replace=True,
            )
            if position.trail_client_algo_id and position.trail_price > 0:
                await self.update_trail(
                    position.position_id, position.trail_price, True, now_ms(), force_replace=True,
                )
            return
        position.status = "closed"
        position.exit_time = position.updated_time
        position.exit_price = exit_notional / closed_quantity if closed_quantity > 0 else delta_price
        self.ledger.save_position(position)
        self.positions.pop(position.position_id, None)
        self.positions_by_symbol.pop(position.symbol, None)
        await self._cancel_position_algos(position)

    async def _cancel_position_algos(
        self,
        position: LivePosition,
        *,
        active_client_ids: set[str] | None = None,
    ) -> None:
        for client_algo_id in (position.trail_client_algo_id, position.structure_client_algo_id):
            if not client_algo_id:
                continue
            if active_client_ids is not None and client_algo_id not in active_client_ids:
                continue
            try:
                await self._cancel_algo_confirmed(client_algo_id)
            except (GatewayError, UnknownExecutionStatus) as exc:
                self.ledger.append_event(now_ms(), "ALGO_CANCEL_FAILED", client_algo_id, {"error": str(exc)})
                self.safe_halt(f"closed_position_algo_cancel_unresolved:{client_algo_id}")

    async def update_trail(
        self, position_id: str, desired_price: Decimal, arm: bool, decision_time: int,
        *, force_replace: bool = False,
    ) -> None:
        position = self.positions.get(position_id)
        if not position or not position.protected:
            return
        old_id = position.trail_client_algo_id
        if not arm or desired_price <= 0:
            if old_id and self.can_send_reduce_orders:
                try:
                    await self._cancel_algo_confirmed(old_id)
                except (GatewayError, UnknownExecutionStatus) as exc:
                    self.ledger.append_event(
                        now_ms(), "TRAIL_CANCEL_UNRESOLVED", old_id, {"error": str(exc)},
                    )
                    self.safe_halt(f"trail_cancel_unresolved:{old_id}")
                    return
            position.trail_client_algo_id = ""
            position.trail_algo_id = None
            position.trail_price = D("0")
            self.ledger.save_position(position)
            return
        rules = self.rules.get(position.symbol)
        desired = rules.price_up(desired_price)
        if old_id and desired == position.trail_price and not force_replace:
            return
        new_id = short_id(
            "tr", f"{position.position_id}:{decision_time}:{desired}:{position.quantity}"
        )
        if not self.can_send_reduce_orders:
            self.ledger.append_event(decision_time, "TRAIL_DRY_RUN", position_id, {"price": str(desired), "arm": True})
            return
        params = {
            "algoType": "CONDITIONAL", "symbol": position.symbol, "side": "BUY",
            "positionSide": self.config.position_side, "type": "STOP_MARKET",
            "quantity": decimal_text(position.quantity), "triggerPrice": decimal_text(desired),
            "workingType": "CONTRACT_PRICE", "priceProtect": "false",
            "clientAlgoId": new_id, "newOrderRespType": "ACK",
        }
        if self.config.exchange_reduce_only:
            params["reduceOnly"] = "true"
        try:
            result = await self._place_algo_confirmed(params)
        except (GatewayError, UnknownExecutionStatus) as exc:
            self.ledger.append_event(now_ms(), "TRAIL_REPLACE_FAILED", position_id, {"error": str(exc)})
            self.safe_halt(f"trail_replace_unresolved:{position.symbol}:{type(exc).__name__}")
            return
        position.trail_algo_id = int(result.get("algoId") or 0) or None
        position.trail_client_algo_id = new_id
        position.trail_price = desired
        position.updated_time = now_ms()
        self.ledger.save_algo({
            "client_algo_id": new_id, "position_id": position.position_id,
            "symbol": position.symbol, "role": "trailing_exit", "algo_id": position.trail_algo_id,
            "status": str(result.get("algoStatus") or "NEW"), "trigger_price": desired,
            "updated_time": position.updated_time, "raw": result,
        })
        self.ledger.save_position(position)
        if old_id:
            try:
                await self._cancel_algo_confirmed(old_id)
            except (GatewayError, UnknownExecutionStatus) as exc:
                self.ledger.append_event(now_ms(), "OLD_TRAIL_CANCEL_FAILED", old_id, {"error": str(exc)})
                self.safe_halt(f"old_trail_cancel_unresolved:{old_id}")

    async def handle_user_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("e") or "")
        event_time = int(payload.get("E") or payload.get("T") or now_ms())
        self.ledger.append_event(event_time, f"USER_{event_type or 'UNKNOWN'}", "", payload)
        if event_type == "ORDER_TRADE_UPDATE":
            await self._handle_order_trade_update(payload.get("o") or {}, event_time)
        elif event_type == "TRADE_LITE":
            client_id = str(payload.get("c") or "")
            waiter = self._event_waiters.get(client_id)
            if waiter:
                waiter.set()
        elif event_type == "ALGO_UPDATE":
            self._handle_algo_update(payload.get("o") or payload, event_time)
        elif event_type == "ACCOUNT_UPDATE":
            self._handle_account_update(payload, event_time)
        elif event_type == "riskLevelChange":
            risk_state = str(payload.get("s") or "UNKNOWN").upper()
            if risk_state in {"MARGIN_CALL", "REDUCE_ONLY", "FORCE_LIQUIDATION"}:
                self.safe_halt(f"risk_level_change:{risk_state.lower()}")
        elif event_type in {"MARGIN_CALL", "listenKeyExpired"}:
            self.safe_halt(event_type.lower())

    async def _handle_order_trade_update(self, data: dict[str, Any], event_time: int) -> None:
        client_id = str(data.get("c") or "")
        order = self.orders.get(client_id)
        if order:
            self._apply_order_result(order, {
                "orderId": data.get("i"), "executedQty": data.get("z"),
                "avgPrice": data.get("ap"), "status": data.get("X"),
                "updateTime": data.get("T") or event_time,
            })
            if order.filled_quantity > order.applied_quantity:
                intent = self._intent_cache.get(order.intent_id) or self.ledger.intent(order.intent_id)
                if intent and order.side == "SELL" and not order.reduce_only:
                    await self._apply_entry_fill(intent, order, self.rules.get(order.symbol))
                elif order.side == "BUY" and order.reduce_only:
                    position = self.positions.get(intent.position_id) if intent else None
                    position = position or self.positions_by_symbol.get(order.symbol)
                    if position:
                        await self._apply_exit_fill(position, order)
        if str(data.get("x")) == "TRADE" and int(data.get("t") or -1) >= 0:
            fill = LiveFill(
                exchange_order_id=int(data.get("i") or 0), trade_id=int(data.get("t") or 0),
                client_order_id=client_id, symbol=str(data.get("s") or ""), side=str(data.get("S") or ""),
                quantity=D(str(data.get("l") or "0")), price=D(str(data.get("L") or "0")),
                commission=D(str(data.get("n") or "0")), commission_asset=str(data.get("N") or ""),
                realized_pnl=D(str(data.get("rp") or "0")), maker=bool(data.get("m")),
                trade_time=int(data.get("T") or event_time),
            )
            self.ledger.save_fill(fill)

    def _handle_algo_update(self, data: dict[str, Any], event_time: int) -> None:
        client_id = str(data.get("caid") or data.get("clientAlgoId") or "")
        position = next(
            (p for p in self.positions.values() if client_id in {p.structure_client_algo_id, p.trail_client_algo_id}),
            None,
        )
        if not position:
            return
        role = "structure_stop" if client_id == position.structure_client_algo_id else "trailing_exit"
        status = str(data.get("X") or data.get("algoStatus") or "UNKNOWN")
        self.ledger.save_algo({
            "client_algo_id": client_id, "position_id": position.position_id,
            "symbol": position.symbol, "role": role, "algo_id": int(data.get("aid") or 0) or None,
            "status": status, "trigger_price": data.get("tp") or "0",
            "updated_time": event_time, "raw": data,
        })

    def _handle_account_update(self, payload: dict[str, Any], event_time: int) -> None:
        if self.config.account_api == "portfolio_margin":
            # Portfolio equity is multi-asset and cannot be reconstructed from
            # a single ACCOUNT_UPDATE balance row. The periodic PAPI reconcile
            # refreshes the authoritative account snapshot.
            return
        account = payload.get("a") or {}
        balances = account.get("B") or []
        usdt = next((row for row in balances if row.get("a") == "USDT"), {})
        if not usdt:
            return
        wallet = D(str(usdt.get("wb") or "0"))
        available = D(str(usdt.get("cw") or wallet))
        snapshot = AccountSnapshot(event_time, wallet, available, wallet, D("0"), D("0"))
        self.set_account(snapshot, payload)

    async def _recover_exit_fills(self, position: LivePosition) -> None:
        """Recover stop/algo exit fills when the position update beats the trade event."""
        rows: list[dict[str, Any]] = []
        for delay in (0.0, 0.10, 0.30):
            if delay:
                await asyncio.sleep(delay)
            try:
                rows = await asyncio.to_thread(
                    self.gateway.rest.user_trades, position.symbol, position.entry_time, 1000,
                )
            except GatewayError as exc:
                self.ledger.append_event(
                    now_ms(), "EXIT_TRADES_QUERY_FAILED", position.position_id,
                    {"symbol": position.symbol, "error": str(exc)},
                )
                continue
            matching = [
                row for row in rows
                if str(row.get("side") or "").upper() == "BUY"
                and str(row.get("positionSide") or self.config.position_side) == self.config.position_side
                and int(row.get("time") or 0) >= position.entry_time
            ]
            for row in matching:
                order_id = int(row.get("orderId") or 0)
                trade_id = int(row.get("id") or row.get("tradeId") or -1)
                if trade_id < 0:
                    continue
                self.ledger.save_fill(LiveFill(
                    exchange_order_id=order_id,
                    trade_id=trade_id,
                    client_order_id=f"recovered-{order_id}",
                    symbol=position.symbol,
                    side="BUY",
                    quantity=D(str(row.get("qty") or "0")),
                    price=D(str(row.get("price") or "0")),
                    commission=D(str(row.get("commission") or "0")),
                    commission_asset=str(row.get("commissionAsset") or ""),
                    realized_pnl=D(str(row.get("realizedPnl") or "0")),
                    maker=bool(row.get("maker")),
                    trade_time=int(row.get("time") or now_ms()),
                ))
            if matching:
                self.ledger.append_event(
                    now_ms(), "EXIT_FILLS_RECOVERED", position.position_id,
                    {"symbol": position.symbol, "fill_count": len(matching)},
                )
                return

    async def reconcile(self, exchange_state: dict[str, Any]) -> dict[str, Any]:
        nonzero_positions = [
            row for row in exchange_state.get("positions", [])
            if D(str(row.get("positionAmt") or "0")) != 0
        ]
        external_sides = sorted(
            f"{row.get('symbol')}:{row.get('positionSide')}"
            for row in nonzero_positions
            if str(row.get("positionSide") or self.config.position_side) != self.config.position_side
        )
        if external_sides:
            self.safe_halt(f"external_position_sides:{external_sides}")
        exchange_positions = {
            str(row.get("symbol")): row
            for row in nonzero_positions
            if str(row.get("positionSide") or self.config.position_side) == self.config.position_side
        }
        open_algo_rows = {
            str(row.get("clientAlgoId") or row.get("caid") or ""): row
            for row in exchange_state.get("open_algo_orders", [])
            if str(row.get("clientAlgoId") or row.get("caid") or "")
        }
        local_symbols = set(self.positions_by_symbol)
        exchange_symbols = set(exchange_positions)
        if exchange_symbols - local_symbols:
            self.safe_halt(f"orphan_exchange_positions:{sorted(exchange_symbols - local_symbols)}")
        for symbol in sorted(local_symbols - exchange_symbols):
            position = self.positions_by_symbol[symbol]
            summary = self.ledger.exit_fill_summary(symbol, position.entry_time)
            if summary["average_price"] <= 0:
                await self._recover_exit_fills(position)
                summary = self.ledger.exit_fill_summary(symbol, position.entry_time)
            if summary["average_price"] <= 0:
                self.safe_halt(f"exchange_flat_exit_fill_missing:{position.position_id}")
            position.status = "closed"
            position.exit_time = now_ms()
            position.exit_price = summary["average_price"]
            position.realized_pnl = summary["realized_pnl"]
            position.updated_time = position.exit_time
            position.metadata["exit_reason"] = "exchange_flat_reconcile"
            self.ledger.save_position(position)
            self.positions.pop(position.position_id, None)
            self.positions_by_symbol.pop(symbol, None)
            self.ledger.append_event(
                position.exit_time, "POSITION_CLOSED_BY_EXCHANGE", position.position_id,
                {"symbol": symbol, "reason": "stop_algo_or_external_reduce"},
            )
            await self._cancel_position_algos(
                position, active_client_ids=set(open_algo_rows),
            )
        exchange_open_orders = list(exchange_state.get("open_orders", []))
        local_order_ids = set(self.orders)
        orphan_owned_orders: list[tuple[str, str]] = []
        external_orders: list[str] = []
        for row in exchange_open_orders:
            client_id = str(row.get("clientOrderId") or row.get("origClientOrderId") or "")
            symbol = str(row.get("symbol") or "")
            if not client_id or client_id in local_order_ids:
                continue
            if client_id.startswith("bh-"):
                orphan_owned_orders.append((symbol, client_id))
            else:
                external_orders.append(f"{symbol}:{client_id}")
        if external_orders:
            self.safe_halt(f"external_open_orders:{sorted(external_orders)}")
        if orphan_owned_orders and self.can_send_reduce_orders:
            for symbol, client_id in orphan_owned_orders:
                try:
                    await self.gateway.trade_ws.cancel_order(symbol, client_id)
                    self.ledger.append_event(
                        now_ms(), "ORPHAN_ORDER_CANCELLED", client_id, {"symbol": symbol},
                    )
                except (GatewayError, UnknownExecutionStatus) as exc:
                    self.safe_halt(f"orphan_order_cancel_failed:{client_id}:{type(exc).__name__}")
        open_algos = set(open_algo_rows)
        known_algos_at_reconcile_start = {
            client_id
            for position in self.positions.values()
            for client_id in (position.structure_client_algo_id, position.trail_client_algo_id)
            if client_id
        }
        for position in self.positions.values():
            row = exchange_positions.get(position.symbol)
            if row:
                position.liquidation_price = D(str(row.get("liquidationPrice") or "0"))
                self.ledger.save_position(position)
            exchange_qty = abs(D(str(row.get("positionAmt") or "0"))) if row else D("0")
            quantity_changed = bool(row and exchange_qty != position.quantity)
            if quantity_changed:
                self.safe_halt(
                    f"position_quantity_mismatch:{position.symbol}:local={position.quantity}:exchange={exchange_qty}"
                )
                old_quantity = position.quantity
                position.quantity = exchange_qty
                exchange_entry = D(str(row.get("entryPrice") or "0"))
                if exchange_entry > 0:
                    position.entry_price = exchange_entry
                position.updated_time = now_ms()
                position.metadata["quantity_reconciled_from"] = str(old_quantity)
                position.metadata["quantity_reconciled_time"] = position.updated_time
                self.ledger.save_position(position)
                self.ledger.append_event(
                    position.updated_time, "POSITION_QUANTITY_RECONCILED", position.position_id,
                    {
                        "symbol": position.symbol,
                        "local_quantity": str(old_quantity),
                        "exchange_quantity": str(exchange_qty),
                    },
                )
            if position.status == "closing":
                pending_reduce = any(
                    str(order.get("symbol") or "") == position.symbol
                    and str(order.get("side") or "").upper() == "BUY"
                    and str(order.get("status") or "NEW").upper() not in TERMINAL_ORDER_STATES
                    for order in exchange_open_orders
                )
                if not pending_reduce:
                    position.status = "open"
                    position.updated_time = now_ms()
                    self.ledger.save_position(position)
            structure_row = open_algo_rows.get(position.structure_client_algo_id) or {}
            structure_quantity = D(str(
                structure_row.get("quantity") or structure_row.get("origQty") or "0"
            ))
            structure_quantity_mismatch = bool(
                structure_row and structure_quantity > 0 and structure_quantity != position.quantity
            )
            if structure_quantity_mismatch:
                self.safe_halt(
                    f"structure_quantity_mismatch:{position.symbol}:"
                    f"algo={structure_quantity}:exchange={position.quantity}"
                )
            replace_structure = quantity_changed or structure_quantity_mismatch
            if position.structure_client_algo_id not in open_algos or replace_structure:
                position.protected = False
                self.ledger.save_position(position)
                try:
                    await self.ensure_structure_protection(
                        position, self.rules.get(position.symbol), force_replace=replace_structure,
                    )
                except Exception as exc:
                    self.safe_halt(f"structure_stop_missing:{position.symbol}:{type(exc).__name__}")
            if quantity_changed and position.trail_client_algo_id and position.trail_price > 0:
                await self.update_trail(
                    position.position_id, position.trail_price, True, now_ms(), force_replace=True,
                )
        known_algos = known_algos_at_reconcile_start | {
            client_id
            for position in self.positions.values()
            for client_id in (position.structure_client_algo_id, position.trail_client_algo_id)
            if client_id
        }
        orphan_owned_algos = sorted(
            client_id for client_id in open_algos
            if client_id.startswith("bh-") and client_id not in known_algos
        )
        external_algos = sorted(
            client_id for client_id in open_algos
            if client_id and not client_id.startswith("bh-") and client_id not in known_algos
        )
        if external_algos:
            self.safe_halt(f"external_algo_orders:{external_algos}")
        if orphan_owned_algos and self.can_send_reduce_orders:
            for client_id in orphan_owned_algos:
                try:
                    await self._cancel_algo_confirmed(client_id)
                    self.ledger.append_event(
                        now_ms(), "ORPHAN_ALGO_CANCELLED", client_id, {},
                    )
                except (GatewayError, UnknownExecutionStatus) as exc:
                    self.safe_halt(f"orphan_algo_cancel_failed:{client_id}:{type(exc).__name__}")
        return {
            "ok": not self.safe_halt_reason,
            "safe_halt_reason": self.safe_halt_reason,
            "local_positions": sorted(local_symbols),
            "exchange_positions": sorted(exchange_symbols),
            "open_algo_orders": len(open_algos),
        }

    def recoverable_halts_after_reconcile(
        self,
        exchange_state: dict[str, Any],
    ) -> set[str]:
        """Return operational halts proven resolved by an exchange snapshot.

        Unknown executions and external positions/orders are deliberately not
        included. They still require manual review.
        """
        reasons = [
            item for item in self.safe_halt_reason.split(" | ") if item
        ]
        if not reasons:
            return set()
        exchange_positions = {
            str(row.get("symbol")): row
            for row in exchange_state.get("positions", [])
            if D(str(row.get("positionAmt") or "0")) != 0
            and str(row.get("positionSide") or self.config.position_side)
            == self.config.position_side
        }
        open_orders = {
            str(row.get("clientOrderId") or row.get("origClientOrderId") or "")
            for row in exchange_state.get("open_orders", [])
        }
        open_algo_rows = {
            str(row.get("clientAlgoId") or row.get("caid") or ""): row
            for row in exchange_state.get("open_algo_orders", [])
            if str(row.get("clientAlgoId") or row.get("caid") or "")
        }

        def structure_ok(symbol: str) -> bool:
            position = self.positions_by_symbol.get(symbol)
            if position is None:
                return symbol not in exchange_positions
            client_id = position.structure_client_algo_id
            row = open_algo_rows.get(client_id)
            if not position.protected or not client_id or row is None:
                return False
            algo_quantity = D(str(
                row.get("quantity") or row.get("origQty") or "0"
            ))
            return algo_quantity <= 0 or algo_quantity == position.quantity

        resolved: set[str] = set()
        cancel_prefixes = {
            "trail_cancel_unresolved:": open_algo_rows,
            "old_trail_cancel_unresolved:": open_algo_rows,
            "old_structure_cancel_unresolved:": open_algo_rows,
            "closed_position_algo_cancel_unresolved:": open_algo_rows,
            "orphan_algo_cancel_failed:": open_algo_rows,
            "orphan_order_cancel_failed:": open_orders,
        }
        for reason in reasons:
            matched_cancel = False
            for prefix, active in cancel_prefixes.items():
                if reason.startswith(prefix):
                    client_id = reason[len(prefix):].split(":", 1)[0]
                    if client_id and client_id not in active:
                        resolved.add(reason)
                    matched_cancel = True
                    break
            if matched_cancel:
                continue
            if reason.startswith("structure_stop_missing:"):
                symbol = reason.split(":", 2)[1]
                if structure_ok(symbol):
                    resolved.add(reason)
                continue
            if reason.startswith("structure_quantity_mismatch:"):
                symbol = reason.split(":", 2)[1]
                if structure_ok(symbol):
                    resolved.add(reason)
                continue
            if reason.startswith("position_quantity_mismatch:"):
                symbol = reason.split(":", 2)[1]
                position = self.positions_by_symbol.get(symbol)
                exchange = exchange_positions.get(symbol)
                if position is None and exchange is None:
                    resolved.add(reason)
                elif position is not None and exchange is not None:
                    exchange_qty = abs(D(str(exchange.get("positionAmt") or "0")))
                    if exchange_qty == position.quantity and structure_ok(symbol):
                        resolved.add(reason)
                continue
            if reason.startswith("trail_replace_unresolved:"):
                symbol = reason.split(":", 2)[1]
                position = self.positions_by_symbol.get(symbol)
                if position is None:
                    resolved.add(reason)
                elif (
                    position.trail_client_algo_id
                    and position.trail_client_algo_id in open_algo_rows
                ):
                    resolved.add(reason)
        return resolved
