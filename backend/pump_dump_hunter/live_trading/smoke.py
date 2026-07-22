from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from .exchange_rules import decimal_text
from .gateway import GatewayError, UnknownExecutionStatus
from .models import IntentAction, TradeIntent
from .oms import quote_from_api, short_id
from .service import LiveRuntime


D = Decimal


async def smoke_limit_cancel(runtime: LiveRuntime, symbol: str) -> dict[str, Any]:
    """Place a post-only SELL well above ask and cancel it immediately."""
    symbol = symbol.upper()
    rules = runtime.rules.get(symbol)
    quote = quote_from_api(await asyncio.to_thread(runtime.gateway.rest.book_ticker, symbol))
    price = rules.price_up(quote.ask_price * D("1.08"))
    cap = D(str(runtime.config.max_notional_usdt))
    quantity = rules.quantity_down(cap / price, market=False)
    minimum = rules.minimum_quantity(price, market=False)
    if quantity < minimum:
        raise RuntimeError(f"max notional is below {symbol} minimum order")
    client_id = short_id("smk", f"limit:{symbol}:{time.time_ns()}")
    params = {
        "symbol": symbol, "side": "SELL", "positionSide": runtime.config.position_side,
        "type": "LIMIT",
        "timeInForce": "GTX", "quantity": decimal_text(quantity), "price": decimal_text(price),
        "newClientOrderId": client_id, "newOrderRespType": "ACK",
    }
    try:
        placed = await runtime.gateway.trade_ws.place_order(params)
    except UnknownExecutionStatus as exc:
        placed = {"status": "UNKNOWN", "error": str(exc)}
    queried = None
    for _attempt in range(3):
        await asyncio.sleep(0.2)
        try:
            queried = await asyncio.to_thread(runtime.gateway.rest.query_order, symbol, client_order_id=client_id)
            break
        except GatewayError:
            continue
    if queried is None:
        runtime.oms.safe_halt(f"smoke_limit_unresolved:{client_id}")
        raise RuntimeError(f"unable to resolve smoke order {client_id}; account halted")
    status = str(queried.get("status") or "").upper()
    if status in {"NEW", "PARTIALLY_FILLED"}:
        try:
            await runtime.gateway.trade_ws.cancel_order(symbol, client_id)
        except UnknownExecutionStatus:
            pass
        for _attempt in range(10):
            await asyncio.sleep(0.2)
            queried = await asyncio.to_thread(
                runtime.gateway.rest.query_order, symbol, client_order_id=client_id,
            )
            status = str(queried.get("status") or "").upper()
            if status not in {"NEW", "PARTIALLY_FILLED"}:
                break
        if status in {"NEW", "PARTIALLY_FILLED"}:
            runtime.oms.safe_halt(f"smoke_limit_cancel_unresolved:{client_id}")
            raise RuntimeError(
                f"smoke order {client_id} is still open; cancel it manually before continuing"
            )
    filled = D(str(queried.get("executedQty") or "0"))
    emergency = None
    if filled > 0:
        emergency_params = {
            "symbol": symbol, "side": "BUY", "positionSide": runtime.config.position_side,
            "type": "MARKET", "quantity": decimal_text(filled),
            "newClientOrderId": short_id("smx", f"limit:{client_id}"), "newOrderRespType": "RESULT",
        }
        if runtime.config.exchange_reduce_only:
            emergency_params["reduceOnly"] = "true"
        emergency = await runtime.gateway.trade_ws.place_order(emergency_params)
    return {
        "symbol": symbol, "client_order_id": client_id, "placed": placed,
        "final": queried, "emergency_flatten": emergency,
    }


async def smoke_roundtrip(runtime: LiveRuntime, symbol: str) -> dict[str, Any]:
    """Use the production OMS for one minimum-risk short open/protect/close cycle."""
    symbol = symbol.upper()
    quote_row, depth = await asyncio.gather(
        asyncio.to_thread(runtime.gateway.rest.book_ticker, symbol),
        asyncio.to_thread(runtime.gateway.rest.depth, symbol, 20),
    )
    quote = quote_from_api(quote_row)
    stamp = int(time.time() * 1000)
    position_id = f"smoke-{symbol}-{stamp}"
    open_intent = TradeIntent(
        intent_id=f"smoke-open-{symbol}-{stamp}", signal_id=f"smoke-open-{stamp}",
        position_id=position_id, strategy="live_smoke_roundtrip", symbol=symbol,
        action=IntentAction.OPEN_SHORT, decision_time=stamp, signal_price=quote.bid_price,
        strategy_stop_price=quote.ask_price * D("1.02"), reason="smoke_roundtrip",
        evidence=("explicit_real_order_smoke",),
    )
    opened = await runtime.oms.handle_intent(open_intent, quote, depth)
    position = runtime.oms.positions.get(position_id)
    if not position:
        return {"open": opened, "close": None}
    await asyncio.sleep(0.5)
    exit_quote = quote_from_api(await asyncio.to_thread(runtime.gateway.rest.book_ticker, symbol))
    close_intent = TradeIntent(
        intent_id=f"smoke-close-{symbol}-{stamp}", signal_id=f"smoke-close-{stamp}",
        position_id=position_id, strategy="live_smoke_roundtrip", symbol=symbol,
        action=IntentAction.CLOSE_SHORT, decision_time=int(time.time() * 1000),
        signal_price=exit_quote.ask_price, strategy_stop_price=position.structure_stop_price,
        reason="smoke_roundtrip_close", evidence=("explicit_real_order_smoke",),
    )
    closed = None
    try:
        closed = await runtime.oms.handle_intent(close_intent, exit_quote)
    finally:
        if symbol in runtime.oms.positions_by_symbol:
            await runtime.oms.emergency_close(runtime.oms.positions_by_symbol[symbol], "smoke_residual_position")
    return {"open": opened, "close": closed}
