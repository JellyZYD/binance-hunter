from __future__ import annotations

from decimal import Decimal
from typing import Any

from .config import LiveTradingConfig
from .exchange_rules import SymbolRules
from .models import AccountSnapshot, BookQuote, RiskDecision, TradeIntent


D = Decimal


def account_snapshot_from_api(account: dict[str, Any], now_ms: int) -> AccountSnapshot:
    return AccountSnapshot(
        snapshot_time=now_ms,
        wallet_balance=D(str(account.get("totalWalletBalance") or account.get("totalCrossWalletBalance") or "0")),
        available_balance=D(str(account.get("availableBalance") or "0")),
        margin_balance=D(str(account.get("totalMarginBalance") or account.get("totalWalletBalance") or "0")),
        unrealized_pnl=D(str(account.get("totalUnrealizedProfit") or "0")),
        total_maintenance_margin=D(str(account.get("totalMaintMargin") or "0")),
    )


def depth_capacity_usdt(depth: dict[str, Any] | None, floor_price: Decimal) -> Decimal:
    if not depth or floor_price <= 0:
        return D("0")
    total = D("0")
    for price_text, qty_text in depth.get("bids", []):
        price = D(str(price_text))
        if price < floor_price:
            break
        total += price * D(str(qty_text))
    return total


class LiveRiskManager:
    def __init__(self, config: LiveTradingConfig):
        self.config = config
        self.sizing_equity = D("0")
        self.sizing_peak_equity = D("0")
        self.sizing_drawdown_pct = D("0")
        self.sizing_factor = D("1")

    def set_sizing_state(
        self,
        *,
        equity: Decimal,
        peak_equity: Decimal,
        drawdown_pct: Decimal,
        factor: Decimal,
    ) -> None:
        self.sizing_equity = max(D("0"), equity)
        self.sizing_peak_equity = max(D("0"), peak_equity)
        self.sizing_drawdown_pct = max(D("0"), drawdown_pct)
        self.sizing_factor = min(D("1"), max(D("0"), factor))

    def evaluate_short_entry(
        self,
        intent: TradeIntent,
        quote: BookQuote,
        rules: SymbolRules,
        account: AccountSnapshot,
        open_position_count: int,
        depth: dict[str, Any] | None = None,
    ) -> RiskDecision:
        if open_position_count >= self.config.max_open_positions:
            return RiskDecision(False, "max_open_positions")
        if self.config.allowed_symbols and intent.symbol not in self.config.allowed_symbols:
            return RiskDecision(False, "symbol_not_allowlisted")
        if rules.status != "TRADING":
            return RiskDecision(False, f"symbol_status_{rules.status or 'unknown'}")
        if rules.contract_type not in {"PERPETUAL", ""}:
            return RiskDecision(False, "not_perpetual")
        if rules.quote_asset != "USDT" or rules.margin_asset != "USDT":
            return RiskDecision(False, "not_usdt_margin")
        entry = quote.bid_price
        # The exchange protection layer always enforces at least a 1.5% stop.
        # Size against that exact stop instead of the possibly tighter strategy
        # stop, otherwise live risk can exceed the configured risk budget.
        stop = max(intent.strategy_stop_price, entry * D("1.015"))
        if entry <= 0 or stop <= entry:
            return RiskDecision(False, "invalid_entry_or_stop")
        if intent.signal_price > 0:
            chase_bps = (intent.signal_price - entry) / intent.signal_price * D("10000")
            if chase_bps > D(str(self.config.max_entry_slippage_bps)):
                return RiskDecision(False, f"price_escaped_{chase_bps:.2f}bps")
        stop_distance = stop / entry - D("1")
        # Fee and execution buffer is intentionally conservative during local validation.
        cost_buffer = D("0.0015") + D(str(self.config.max_entry_slippage_bps)) / D("10000")
        effective_risk = stop_distance + cost_buffer
        equity = max(D("0"), account.margin_balance)
        available = max(D("0"), account.available_balance)
        if equity <= 0 or available <= 0:
            return RiskDecision(False, "no_available_balance")
        if self.config.sizing_mode == "realized_drawdown_ladder":
            if self.sizing_equity <= 0:
                return RiskDecision(False, "sizing_state_missing", stop_distance_pct=stop_distance)
            sizing_equity = self.sizing_equity
            margin_fraction = min(
                D(str(self.config.margin_fraction_cap)),
                D(str(self.config.base_margin_fraction)) * self.sizing_factor,
            )
            target_margin = min(sizing_equity * margin_fraction, available)
            sizing_notional = target_margin * D(str(self.config.leverage))
        else:
            sizing_equity = equity
            margin_fraction = D(str(self.config.margin_fraction_cap))
            risk_budget = equity * D(str(self.config.risk_per_trade))
            risk_notional = risk_budget / effective_risk
            margin_cap_notional = min(equity * margin_fraction, available) * D(str(self.config.leverage))
            sizing_notional = min(risk_notional, margin_cap_notional)
        absolute_cap = D(str(self.config.max_notional_usdt))
        floor_price = entry * (D("1") - D(str(self.config.max_entry_slippage_bps)) / D("10000"))
        capacity = depth_capacity_usdt(depth, floor_price)
        caps = [sizing_notional, absolute_cap]
        if capacity > 0:
            # Never consume more than 20% of displayed depth inside the slippage band.
            caps.append(capacity * D("0.20"))
        notional = min(caps)
        quantity = rules.quantity_down(notional / entry, market=True)
        minimum = rules.minimum_quantity(entry, market=True)
        if quantity < minimum:
            minimum_notional = minimum * entry
            return RiskDecision(
                False,
                f"minimum_order_exceeds_risk_cap:{minimum_notional}",
                stop_distance_pct=stop_distance,
                sizing_equity=sizing_equity,
                margin_fraction=margin_fraction,
                drawdown_pct=self.sizing_drawdown_pct,
                sizing_factor=self.sizing_factor,
            )
        valid, reason = rules.validate_quantity(quantity, entry, market=True)
        if not valid:
            return RiskDecision(
                False, reason, stop_distance_pct=stop_distance,
                sizing_equity=sizing_equity, margin_fraction=margin_fraction,
                drawdown_pct=self.sizing_drawdown_pct, sizing_factor=self.sizing_factor,
            )
        notional = quantity * entry
        estimated_risk = notional * effective_risk
        # Conservative pre-trade liquidation approximation. Actual liquidation price is checked after fill.
        if not (
            self.config.sizing_mode == "realized_drawdown_ladder"
            and self.config.account_api == "portfolio_margin"
        ):
            approx_liq_distance = D("1") / D(str(self.config.leverage)) - D("0.02")
            required_distance = stop_distance + D(str(self.config.liquidation_stop_buffer_pct))
            if approx_liq_distance <= required_distance:
                return RiskDecision(
                    False, "approx_liquidation_too_close", stop_distance_pct=stop_distance,
                    sizing_equity=sizing_equity, margin_fraction=margin_fraction,
                    drawdown_pct=self.sizing_drawdown_pct, sizing_factor=self.sizing_factor,
                )
        return RiskDecision(
            True, "approved", quantity, notional, estimated_risk, stop_distance,
            sizing_equity, margin_fraction, self.sizing_drawdown_pct, self.sizing_factor,
        )

    def actual_liquidation_is_safe(
        self, entry_price: Decimal, stop_price: Decimal, liquidation_price: Decimal
    ) -> bool:
        if entry_price <= 0 or stop_price <= entry_price:
            return False
        if liquidation_price <= 0:
            return True
        minimum = stop_price * (D("1") + D(str(self.config.liquidation_stop_buffer_pct)))
        return liquidation_price >= minimum
