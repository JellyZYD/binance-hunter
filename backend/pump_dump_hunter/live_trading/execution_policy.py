from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .config import LiveTradingConfig
from .exchange_rules import SymbolRules, decimal_text
from .models import BookQuote, TradeIntent


@dataclass(frozen=True)
class OrderPlan:
    policy: str
    initial_params: dict[str, Any]
    fallback_policy: str = ""
    fallback_params: dict[str, Any] | None = None
    maker_wait_ms: int = 0


class ExecutionPolicyRouter:
    def __init__(self, config: LiveTradingConfig):
        self.config = config

    def choose(self, intent: TradeIntent) -> str:
        if self.config.execution_policy != "randomized":
            return self.config.execution_policy
        digest = hashlib.sha256(intent.intent_id.encode("utf-8")).digest()
        return ("market", "ioc", "maker_first")[digest[0] % 3]

    def maker_wait(self, intent: TradeIntent) -> int:
        candidates = self.config.maker_wait_candidates_ms
        if not candidates:
            return self.config.maker_wait_ms
        digest = hashlib.sha256(intent.intent_id.encode("utf-8")).digest()
        return int(candidates[digest[1] % len(candidates)])

    def open_short(
        self,
        intent: TradeIntent,
        quantity: Decimal,
        quote: BookQuote,
        rules: SymbolRules,
        client_order_id: str,
    ) -> OrderPlan:
        policy = self.choose(intent)
        base = {
            "symbol": intent.symbol,
            "side": "SELL",
            "positionSide": self.config.position_side,
            "quantity": decimal_text(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if policy == "market":
            return OrderPlan(policy, {**base, "type": "MARKET"})
        floor = rules.price_down(
            quote.bid_price * (Decimal("1") - Decimal(str(self.config.ioc_slippage_bps)) / Decimal("10000"))
        )
        ioc = {**base, "type": "LIMIT", "timeInForce": "IOC", "price": decimal_text(floor)}
        if policy == "ioc":
            return OrderPlan(policy, ioc)
        maker_price = rules.price_up(quote.ask_price)
        maker = {**base, "type": "LIMIT", "timeInForce": "GTX", "price": decimal_text(maker_price), "newOrderRespType": "ACK"}
        fallback = {**base, "type": "MARKET", "newClientOrderId": f"{client_order_id[:-2]}fm"}
        return OrderPlan("maker_first", maker, "market", fallback, self.maker_wait(intent))

    def close_short(
        self,
        intent: TradeIntent,
        quantity: Decimal,
        quote: BookQuote,
        rules: SymbolRules,
        client_order_id: str,
        urgent: bool,
    ) -> OrderPlan:
        base = {
            "symbol": intent.symbol,
            "side": "BUY",
            "positionSide": self.config.position_side,
            "quantity": decimal_text(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if self.config.exchange_reduce_only:
            base["reduceOnly"] = "true"
        if urgent:
            return OrderPlan("market", {**base, "type": "MARKET"})
        policy = self.choose(intent)
        if policy == "market":
            return OrderPlan(policy, {**base, "type": "MARKET"})
        ceiling = rules.price_up(
            quote.ask_price * (Decimal("1") + Decimal(str(self.config.ioc_slippage_bps)) / Decimal("10000"))
        )
        ioc = {**base, "type": "LIMIT", "timeInForce": "IOC", "price": decimal_text(ceiling)}
        if policy == "ioc":
            fallback = {**base, "type": "MARKET", "newClientOrderId": f"{client_order_id[:-2]}fm"}
            return OrderPlan(policy, ioc, "market", fallback)
        maker = {
            **base,
            "type": "LIMIT",
            "timeInForce": "GTX",
            "price": decimal_text(rules.price_down(quote.bid_price)),
            "newOrderRespType": "ACK",
        }
        fallback = {**base, "type": "MARKET", "newClientOrderId": f"{client_order_id[:-2]}fm"}
        return OrderPlan("maker_first", maker, "market", fallback, min(self.maker_wait(intent), 250))
