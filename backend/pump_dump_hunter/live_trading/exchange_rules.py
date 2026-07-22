from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any


ZERO = Decimal("0")


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _step_round(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=rounding)
    return units * step


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    margin_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    lot_step: Decimal
    lot_min: Decimal
    lot_max: Decimal
    market_step: Decimal
    market_min: Decimal
    market_max: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_symbol(cls, row: dict[str, Any]) -> "SymbolRules":
        filters = {str(item.get("filterType")): item for item in row.get("filters", [])}
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        market = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
        return cls(
            symbol=str(row["symbol"]),
            status=str(row.get("status") or row.get("contractStatus") or ""),
            contract_type=str(row.get("contractType") or ""),
            quote_asset=str(row.get("quoteAsset") or ""),
            margin_asset=str(row.get("marginAsset") or ""),
            tick_size=Decimal(str(price.get("tickSize") or "0")),
            min_price=Decimal(str(price.get("minPrice") or "0")),
            max_price=Decimal(str(price.get("maxPrice") or "0")),
            lot_step=Decimal(str(lot.get("stepSize") or "0")),
            lot_min=Decimal(str(lot.get("minQty") or "0")),
            lot_max=Decimal(str(lot.get("maxQty") or "0")),
            market_step=Decimal(str(market.get("stepSize") or "0")),
            market_min=Decimal(str(market.get("minQty") or "0")),
            market_max=Decimal(str(market.get("maxQty") or "0")),
            min_notional=Decimal(str(notional.get("notional") or notional.get("minNotional") or "0")),
        )

    def price_down(self, value: Decimal) -> Decimal:
        return _step_round(value, self.tick_size, ROUND_FLOOR)

    def price_up(self, value: Decimal) -> Decimal:
        return _step_round(value, self.tick_size, ROUND_CEILING)

    def quantity_down(self, value: Decimal, market: bool = True) -> Decimal:
        step = self.market_step if market else self.lot_step
        return _step_round(value, step, ROUND_FLOOR)

    def minimum_quantity(self, reference_price: Decimal, market: bool = True) -> Decimal:
        if reference_price <= 0:
            return ZERO
        min_qty = self.market_min if market else self.lot_min
        step = self.market_step if market else self.lot_step
        by_notional = _step_round(self.min_notional / reference_price, step, ROUND_CEILING)
        return max(min_qty, by_notional)

    def validate_quantity(self, quantity: Decimal, reference_price: Decimal, market: bool = True) -> tuple[bool, str]:
        minimum = self.market_min if market else self.lot_min
        maximum = self.market_max if market else self.lot_max
        step = self.market_step if market else self.lot_step
        if quantity < minimum:
            return False, f"quantity below minQty {decimal_text(minimum)}"
        if maximum > 0 and quantity > maximum:
            return False, f"quantity above maxQty {decimal_text(maximum)}"
        if step > 0 and quantity != self.quantity_down(quantity, market=market):
            return False, f"quantity not aligned to stepSize {decimal_text(step)}"
        if reference_price > 0 and quantity * reference_price < self.min_notional:
            return False, f"notional below minimum {decimal_text(self.min_notional)}"
        return True, "ok"


class ExchangeRules:
    def __init__(self, exchange_info: dict[str, Any]):
        self.rate_limits = list(exchange_info.get("rateLimits", []))
        self.symbols = {
            str(row.get("symbol")): SymbolRules.from_exchange_symbol(row)
            for row in exchange_info.get("symbols", [])
            if row.get("symbol")
        }

    def get(self, symbol: str) -> SymbolRules:
        try:
            return self.symbols[symbol.upper()]
        except KeyError as exc:
            raise KeyError(f"symbol missing from exchangeInfo: {symbol}") from exc
