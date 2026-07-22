from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class IntentAction(str, Enum):
    OPEN_SHORT = "open_short"
    CLOSE_SHORT = "close_short"


class OrderState(str, Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTING = "SUBMITTING"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTING = "PROTECTING"
    PROTECTED = "PROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    EXCHANGE_REJECTED = "EXCHANGE_REJECTED"
    UNKNOWN = "UNKNOWN"
    SAFE_HALT = "SAFE_HALT"


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    signal_id: str
    position_id: str
    strategy: str
    symbol: str
    action: IntentAction
    decision_time: int
    signal_price: Decimal
    strategy_stop_price: Decimal
    reason: str
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["action"] = self.action.value
        row["signal_price"] = str(self.signal_price)
        row["strategy_stop_price"] = str(self.strategy_stop_price)
        row["evidence"] = list(self.evidence)
        return row


@dataclass
class LiveOrder:
    client_order_id: str
    intent_id: str
    symbol: str
    side: str
    order_type: str
    execution_policy: str
    state: OrderState
    quantity: Decimal
    price: Decimal = Decimal("0")
    reduce_only: bool = False
    exchange_order_id: int | None = None
    filled_quantity: Decimal = Decimal("0")
    applied_quantity: Decimal = Decimal("0")
    applied_notional: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    reference_price: Decimal = Decimal("0")
    arrival_price: Decimal = Decimal("0")
    created_time: int = 0
    submit_time: int = 0
    ack_time: int = 0
    first_fill_time: int = 0
    final_fill_time: int = 0
    slippage_bps: Decimal = Decimal("0")
    arrival_slippage_bps: Decimal = Decimal("0")
    updated_time: int = 0
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["state"] = self.state.value
        for key in (
            "quantity", "price", "filled_quantity", "applied_quantity", "applied_notional",
            "average_price", "reference_price", "arrival_price", "slippage_bps",
            "arrival_slippage_bps",
        ):
            row[key] = str(row[key])
        return row


@dataclass(frozen=True)
class LiveFill:
    exchange_order_id: int
    trade_id: int
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    maker: bool
    trade_time: int

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("quantity", "price", "commission", "realized_pnl"):
            row[key] = str(row[key])
        return row


@dataclass
class LivePosition:
    position_id: str
    intent_id: str
    symbol: str
    status: str
    quantity: Decimal
    entry_price: Decimal
    structure_stop_price: Decimal
    trail_price: Decimal = Decimal("0")
    liquidation_price: Decimal = Decimal("0")
    entry_time: int = 0
    exit_time: int = 0
    exit_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    entry_client_order_id: str = ""
    structure_algo_id: int | None = None
    structure_client_algo_id: str = ""
    trail_algo_id: int | None = None
    trail_client_algo_id: str = ""
    protected: bool = False
    updated_time: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in (
            "quantity", "entry_price", "structure_stop_price", "trail_price",
            "liquidation_price", "exit_price", "realized_pnl",
        ):
            row[key] = str(row[key])
        return row


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_time: int
    wallet_balance: Decimal
    available_balance: Decimal
    margin_balance: Decimal
    unrealized_pnl: Decimal
    total_maintenance_margin: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    quantity: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    risk_usdt: Decimal = Decimal("0")
    stop_distance_pct: Decimal = Decimal("0")
    sizing_equity: Decimal = Decimal("0")
    margin_fraction: Decimal = Decimal("0")
    drawdown_pct: Decimal = Decimal("0")
    sizing_factor: Decimal = Decimal("1")


@dataclass(frozen=True)
class BookQuote:
    symbol: str
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    event_time: int

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid_price
        return (self.ask_price - self.bid_price) / mid * Decimal("10000") if mid > 0 else Decimal("0")
