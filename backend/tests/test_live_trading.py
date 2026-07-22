from __future__ import annotations

import asyncio
import copy
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from pump_dump_hunter.live_trading.config import LiveTradingConfig
from pump_dump_hunter.live_trading.credentials import BinanceCredentials
from pump_dump_hunter.live_trading.exchange_rules import ExchangeRules
from pump_dump_hunter.live_trading.gateway import (
    BinanceSignedRest,
    GatewayError,
    GatewayResponse,
    UnknownExecutionStatus,
)
from pump_dump_hunter.live_trading.ledger import LiveLedger
from pump_dump_hunter.live_trading.models import (
    AccountSnapshot,
    BookQuote,
    IntentAction,
    LiveOrder,
    OrderState,
    TradeIntent,
)
from pump_dump_hunter.live_trading.notifier import LiveEventNotifier
from pump_dump_hunter.live_trading.oms import LiveOrderManager
from pump_dump_hunter.live_trading.service import (
    ClaudeLiveTradingService,
    consume_order_nonce,
    fetch_reconcile_inputs,
    issue_order_nonce,
    recoverable_connectivity_halts,
    signal_to_intent,
)
from pump_dump_hunter.waterfall import WaterfallSignal
from tests.helpers import temp_settings


D = Decimal


def exchange_info() -> dict:
    return {
        "symbols": [{
            "symbol": "ALTUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "100000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]
    }


def live_config(root: Path, *, mode: str = "live_micro", policy: str = "market") -> LiveTradingConfig:
    settings = copy.deepcopy(temp_settings())
    settings["live_trading"] = {
        "enabled": mode not in {"paper", "dry_run"},
        "mode": mode,
        "real_order_enabled": mode not in {"paper", "dry_run"},
        "ledger_path": str(root / "live.db"),
        "leverage": 3,
        "max_open_positions": 1,
        "risk_per_trade": 0.0025,
        "margin_fraction_cap": 0.05,
        "max_notional_usdt": 20,
        "execution_policy": policy,
    }
    return LiveTradingConfig.from_settings(settings)


def ladder_live_config(root: Path) -> LiveTradingConfig:
    settings = copy.deepcopy(temp_settings())
    settings["live_trading"] = {
        "enabled": True,
        "mode": "live_micro",
        "real_order_enabled": True,
        "ledger_path": str(root / "ladder_live.db"),
        "account_api": "portfolio_margin",
        "position_mode": "hedge",
        "leverage": 10,
        "max_open_positions": 1,
        "sizing_mode": "realized_drawdown_ladder",
        "base_margin_fraction": 0.10,
        "drawdown_ladder": [
            {"below": 0.05, "factor": 1.0},
            {"below": 0.10, "factor": 0.75},
            {"below": 0.15, "factor": 0.50},
            {"below": None, "factor": 0.25},
        ],
        "risk_per_trade": 0.0025,
        "margin_fraction_cap": 0.10,
        "max_notional_usdt": 200,
        "execution_policy": "market",
    }
    return LiveTradingConfig.from_settings(settings)


def intent(action: IntentAction = IntentAction.OPEN_SHORT, suffix: str = "1") -> TradeIntent:
    return TradeIntent(
        intent_id=f"intent-{suffix}", signal_id=f"signal-{suffix}", position_id="position-1",
        strategy="claude_board_wf_1m", symbol="ALTUSDT", action=action,
        decision_time=1_700_000_000_000, signal_price=D("100"),
        strategy_stop_price=D("102"), reason="strategy_entry" if action == IntentAction.OPEN_SHORT else "take_profit_trailing",
    )


class FakeRest:
    def __init__(self):
        self.leverage_calls = 0
        self.query_result = None
        self.algo_query_result = {"algoStatus": "CANCELED"}
        self.trades = []

    def set_leverage(self, _symbol, _leverage):
        self.leverage_calls += 1
        return {}

    def set_margin_type(self, _symbol, _margin):
        return {}

    def position_risk(self, _symbol=None):
        return [{"symbol": "ALTUSDT", "positionAmt": "-0.1", "liquidationPrice": "150"}]

    def query_order(self, *_args, **_kwargs):
        if self.query_result is not None:
            return dict(self.query_result)
        raise GatewayError("not found", code=-2013, status=400)

    def user_trades(self, *_args, **_kwargs):
        return [dict(row) for row in self.trades]

    def query_algo_order(self, *_args, **_kwargs):
        return dict(self.algo_query_result)


class FakeTrade:
    def __init__(self, *, protection_fails: bool = False):
        self.protection_fails = protection_fails
        self.order_calls: list[dict] = []
        self.order_cancel_calls: list[tuple[str, str]] = []
        self.algo_calls: list[dict] = []
        self.algo_cancel_calls: list[str] = []
        self.algo_cancel_result = {"algoStatus": "CANCELED"}

    async def place_order(self, params):
        self.order_calls.append(dict(params))
        qty = str(params.get("quantity") or "0")
        return {
            "orderId": len(self.order_calls), "status": "FILLED", "executedQty": qty,
            "avgPrice": "100" if params["side"] == "SELL" else "99",
        }

    async def place_algo(self, params):
        self.algo_calls.append(dict(params))
        if self.protection_fails:
            raise GatewayError("algo rejected", code=-1, status=400)
        return {"algoId": len(self.algo_calls), "algoStatus": "NEW"}

    async def cancel_algo(self, _client_id):
        self.algo_cancel_calls.append(_client_id)
        return dict(self.algo_cancel_result)

    async def cancel_order(self, _symbol, _client_id):
        self.order_cancel_calls.append((_symbol, _client_id))
        return {"status": "CANCELED", "executedQty": "0", "avgPrice": "0"}


class FakeGateway:
    def __init__(self, *, protection_fails: bool = False):
        self.rest = FakeRest()
        self.trade_ws = FakeTrade(protection_fails=protection_fails)


class LiveTradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hunter_live_test_"))
        self.rules = ExchangeRules(exchange_info())
        self.quote = BookQuote("ALTUSDT", D("100"), D("10"), D("100.1"), D("10"), 1_700_000_000_000)
        self.account = AccountSnapshot(1, D("100"), D("100"), D("100"), D("0"), D("0"))

    def manager(self, *, mode="live_micro", policy="market", protection_fails=False, authorized=True):
        cfg = live_config(self.root, mode=mode, policy=policy)
        ledger = LiveLedger(cfg.ledger_path)
        gateway = FakeGateway(protection_fails=protection_fails)
        manager = LiveOrderManager(cfg, gateway, ledger, self.rules, orders_authorized=authorized)
        manager.set_account(self.account)
        return manager, gateway, ledger

    def test_dry_run_never_calls_exchange_order_channel(self) -> None:
        manager, gateway, ledger = self.manager(mode="dry_run", authorized=False)
        first = asyncio.run(manager.handle_intent(intent(), self.quote))
        second = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertEqual(first["status"], "dry_run")
        self.assertEqual(second["status"], "duplicate_intent")
        self.assertEqual(gateway.trade_ws.order_calls, [])
        self.assertIsNotNone(ledger.order(next(iter(manager.orders))))

    def test_symbol_configuration_failure_is_not_left_as_inflight_order(self) -> None:
        manager, gateway, ledger = self.manager()

        def reject_leverage(_symbol, _leverage):
            raise GatewayError("leverage rejected", code=-4000, status=400)

        gateway.rest.set_leverage = reject_leverage
        result = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertEqual(result["status"], "configuration_failed")
        stored = ledger.order(result["order"]["client_order_id"])
        self.assertEqual(stored["state"], "EXCHANGE_REJECTED")
        self.assertEqual(ledger.pending_orders(), [])
        self.assertEqual(gateway.trade_ws.order_calls, [])
        self.assertTrue(manager.safe_halt_reason.startswith("symbol_configuration_failed:"))

    def test_portfolio_margin_config_uses_papi_and_hedge_mode(self) -> None:
        config = live_config(self.root)
        self.assertEqual(config.account_api, "portfolio_margin")
        self.assertEqual(config.position_mode, "hedge")
        self.assertEqual(config.position_side, "SHORT")
        self.assertFalse(config.exchange_reduce_only)
        self.assertEqual(config.rest_base_url, "https://papi.binance.com")
        self.assertEqual(config.market_base_url, "https://fapi.binance.com")

    def test_live_config_accepts_binance_supported_ten_second_recv_window(self) -> None:
        settings = copy.deepcopy(temp_settings())
        settings["live_trading"] = {
            "enabled": True,
            "mode": "live_micro",
            "real_order_enabled": True,
            "ledger_path": str(self.root / "recv-window.db"),
            "recv_window_ms": 10_000,
        }
        self.assertEqual(LiveTradingConfig.from_settings(settings).recv_window_ms, 10_000)

    def test_portfolio_gateway_uses_papi_private_and_fapi_market_routes(self) -> None:
        config = live_config(self.root)
        rest = BinanceSignedRest(config, BinanceCredentials("key", "secret"))
        calls: list[tuple[str, str, str]] = []

        def fake_request(method, path, params=None, **kwargs):
            calls.append((method, path, str(kwargs.get("base_url") or rest.base_url)))
            if path == "/papi/v1/account":
                return GatewayResponse({
                    "actualEquity": "10", "accountEquity": "11",
                    "totalAvailableBalance": "9", "accountMaintMargin": "0.1",
                }, 200, {})
            if path == "/papi/v1/balance":
                return GatewayResponse([{
                    "asset": "USDT", "totalWalletBalance": "10",
                    "umUnrealizedPNL": "1", "cmUnrealizedPNL": "0",
                }], 200, {})
            return GatewayResponse([], 200, {})

        rest._request = fake_request
        account = rest.account_info()
        rest.exchange_info()
        rest.position_risk("ALTUSDT")
        rest.open_algo_orders("ALTUSDT")
        self.assertEqual(account["totalMarginBalance"], "11")
        self.assertEqual(account["availableBalance"], "9")
        self.assertIn(("GET", "/papi/v1/account", "https://papi.binance.com"), calls)
        self.assertIn(("GET", "/papi/v1/um/positionRisk", "https://papi.binance.com"), calls)
        self.assertIn(("GET", "/papi/v1/um/algo/openAlgoOrders", "https://papi.binance.com"), calls)
        self.assertIn(("GET", "/fapi/v1/exchangeInfo", "https://fapi.binance.com"), calls)

    def test_time_sync_uses_response_arrival_bound_not_rtt_midpoint(self) -> None:
        rest = BinanceSignedRest(live_config(self.root), BinanceCredentials("key", "secret"))
        rest._request = lambda *_args, **_kwargs: GatewayResponse(
            {"serverTime": 102_000}, 200, {},
        )
        with patch("pump_dump_hunter.live_trading.gateway.time.time", side_effect=[100.0, 104.0]):
            offset = rest.sync_time()
        self.assertEqual(offset, -2_000)

    def test_read_request_retries_network_failure_and_reports_endpoint(self) -> None:
        rest = BinanceSignedRest(live_config(self.root), BinanceCredentials("key", "secret"))

        class BrokenSession:
            calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                raise requests.ConnectionError("tls eof")

        session = BrokenSession()
        rest._session = lambda: session
        rest._discard_thread_session = lambda: None
        with patch("pump_dump_hunter.live_trading.gateway.time.sleep"), patch(
            "pump_dump_hunter.live_trading.gateway.random.uniform", return_value=0.0,
        ):
            with self.assertRaises(GatewayError) as raised:
                rest._request("GET", "/papi/v1/account", signed=True)
        self.assertEqual(session.calls, 4)
        self.assertEqual(raised.exception.endpoint, "/papi/v1/account")

    def test_rest_session_is_reused_within_worker_thread(self) -> None:
        rest = BinanceSignedRest(live_config(self.root), BinanceCredentials("key", "secret"))
        first = rest._session()
        self.assertIs(first, rest._session())
        rest.close()

    def test_hedge_orders_and_algos_use_short_side_without_reduce_only(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        entry = gateway.trade_ws.order_calls[0]
        stop = gateway.trade_ws.algo_calls[0]
        self.assertEqual(entry["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", entry)
        self.assertEqual(stop["positionSide"], "SHORT")
        self.assertEqual(stop["quantity"], entry["quantity"])
        self.assertNotIn("closePosition", stop)
        self.assertNotIn("reduceOnly", stop)
        asyncio.run(manager.handle_intent(intent(IntentAction.CLOSE_SHORT, "hedge-exit"), self.quote))
        exit_order = gateway.trade_ws.order_calls[-1]
        self.assertEqual(exit_order["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", exit_order)

    def test_portfolio_market_ack_is_queried_until_filled(self) -> None:
        manager, gateway, _ledger = self.manager()

        async def asynchronous_market(params):
            gateway.trade_ws.order_calls.append(dict(params))
            quantity = str(params.get("quantity") or "0")
            gateway.rest.query_result = {
                "orderId": 91, "status": "FILLED", "executedQty": quantity, "avgPrice": "100",
            }
            return {"orderId": 91, "status": "NEW", "executedQty": "0"}

        gateway.trade_ws.place_order = asynchronous_market
        result = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertEqual(result["status"], "filled")
        self.assertEqual(manager.positions["position-1"].entry_price, D("100"))
        self.assertTrue(manager.positions["position-1"].protected)

    def test_missing_order_average_price_recovers_vwap_from_user_trades(self) -> None:
        manager, gateway, ledger = self.manager()

        async def no_average_price(params):
            gateway.trade_ws.order_calls.append(dict(params))
            quantity = D(str(params.get("quantity") or "0"))
            gateway.rest.query_result = {
                "orderId": 92, "status": "FILLED", "executedQty": str(quantity),
            }
            gateway.rest.trades = [
                {
                    "orderId": 92, "id": 7, "side": "SELL", "qty": str(quantity / D("2")),
                    "price": "99", "commission": "0.001", "commissionAsset": "USDT",
                    "realizedPnl": "0", "maker": False, "time": 1_700_000_000_100,
                },
                {
                    "orderId": 92, "id": 8, "side": "SELL", "qty": str(quantity / D("2")),
                    "price": "101", "commission": "0.001", "commissionAsset": "USDT",
                    "realizedPnl": "0", "maker": False, "time": 1_700_000_000_200,
                },
            ]
            return {"orderId": 92, "status": "FILLED", "executedQty": str(quantity)}

        gateway.trade_ws.place_order = no_average_price
        result = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertEqual(result["status"], "filled")
        self.assertEqual(manager.positions["position-1"].entry_price, D("100"))
        self.assertEqual(ledger.order(next(iter(manager.orders)))["average_price"], "100")

    def test_unresolved_market_entry_halts_and_flattens_exchange_position(self) -> None:
        manager, gateway, _ledger = self.manager()
        original = gateway.trade_ws.place_order
        attempts = 0

        async def unknown_then_flatten(params):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                gateway.trade_ws.order_calls.append(dict(params))
                raise UnknownExecutionStatus("submit response lost")
            return await original(params)

        gateway.trade_ws.place_order = unknown_then_flatten
        result = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(manager.safe_halt_reason.startswith("unknown_order_unresolved:"))
        self.assertEqual([row["side"] for row in gateway.trade_ws.order_calls], ["SELL", "BUY"])

    def test_partial_fill_is_applied_exactly_once_and_survives_ledger(self) -> None:
        manager, _gateway, ledger = self.manager()
        order = LiveOrder(
            client_order_id="partial-entry", intent_id="intent-1", symbol="ALTUSDT",
            side="SELL", order_type="LIMIT", execution_policy="maker_first",
            state=OrderState.PARTIALLY_FILLED, quantity=D("0.2"), filled_quantity=D("0.1"),
            average_price=D("100"), created_time=1, updated_time=1,
        )
        manager.orders[order.client_order_id] = order
        asyncio.run(manager._apply_entry_fill(intent(), order, self.rules.get("ALTUSDT")))
        asyncio.run(manager._apply_entry_fill(intent(), order, self.rules.get("ALTUSDT")))
        self.assertEqual(manager.positions["position-1"].quantity, D("0.1"))
        self.assertEqual(order.applied_quantity, D("0.1"))
        self.assertEqual(D(ledger.order("partial-entry")["applied_quantity"]), D("0.1"))

    def test_cumulative_partial_average_applies_only_incremental_notional(self) -> None:
        manager, _gateway, _ledger = self.manager()
        order = LiveOrder(
            client_order_id="two-fills", intent_id="intent-1", symbol="ALTUSDT",
            side="SELL", order_type="LIMIT", execution_policy="maker_first",
            state=OrderState.PARTIALLY_FILLED, quantity=D("0.2"), filled_quantity=D("0.1"),
            average_price=D("100"), created_time=1, updated_time=1,
        )
        asyncio.run(manager._apply_entry_fill(intent(), order, self.rules.get("ALTUSDT")))
        order.filled_quantity = D("0.2")
        order.average_price = D("105")  # second 0.1 fill is actually at 110
        order.state = OrderState.FILLED
        asyncio.run(manager._apply_entry_fill(intent(), order, self.rules.get("ALTUSDT")))
        self.assertEqual(manager.positions["position-1"].quantity, D("0.2"))
        self.assertEqual(manager.positions["position-1"].entry_price, D("105"))
        self.assertEqual(order.applied_notional, D("21.0"))

    def test_protection_failure_immediately_flattens_and_halts(self) -> None:
        manager, gateway, _ledger = self.manager(protection_fails=True)
        result = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertIsNone(result.get("position"))
        self.assertNotIn("ALTUSDT", manager.positions_by_symbol)
        self.assertTrue(manager.safe_halt_reason.startswith("emergency_close:"))
        self.assertEqual([row["side"] for row in gateway.trade_ws.order_calls], ["SELL", "BUY"])

    def test_exit_partial_fill_does_not_close_full_position(self) -> None:
        manager, _gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions["position-1"]
        original = position.quantity
        close_order = LiveOrder(
            client_order_id="partial-close", intent_id="exit-1", symbol="ALTUSDT",
            side="BUY", order_type="LIMIT", execution_policy="ioc",
            state=OrderState.PARTIALLY_FILLED, quantity=original,
            filled_quantity=original / D("2"), average_price=D("99"), reduce_only=True,
        )
        asyncio.run(manager._apply_exit_fill(position, close_order))
        self.assertEqual(position.status, "open")
        self.assertEqual(position.quantity, original / D("2"))
        asyncio.run(manager._apply_exit_fill(position, close_order))
        self.assertEqual(position.quantity, original / D("2"))

    def test_nonce_is_short_lived_and_single_use(self) -> None:
        path = self.root / "nonce.db"
        issued = issue_order_nonce(path, ttl_seconds=60)
        self.assertTrue(consume_order_nonce(path, issued["nonce"]))
        self.assertFalse(consume_order_nonce(path, issued["nonce"]))

    def test_nonce_consumption_is_atomic_across_processes(self) -> None:
        path = self.root / "nonce-race.db"
        issued = issue_order_nonce(path, ttl_seconds=60)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _index: consume_order_nonce(path, issued["nonce"]),
                range(8),
            ))
        self.assertEqual(results.count(True), 1)

    def test_risk_sizes_against_actual_minimum_structure_stop(self) -> None:
        manager, _gateway, _ledger = self.manager()
        tight = TradeIntent(
            intent_id="tight-stop", signal_id="tight-stop", position_id="tight-stop",
            strategy="claude_board_wf_1m", symbol="ALTUSDT",
            action=IntentAction.OPEN_SHORT, decision_time=1,
            signal_price=D("100"), strategy_stop_price=D("100.5"),
            reason="strategy_entry",
        )
        decision = manager.risk.evaluate_short_entry(
            tight, self.quote, self.rules.get("ALTUSDT"), self.account, 0,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.stop_distance_pct, D("0.015"))
        self.assertLess(decision.quantity, D("0.2"))

    def test_unknown_cancel_never_falls_through_to_market_fallback(self) -> None:
        manager, gateway, _ledger = self.manager(policy="maker_first")
        order = LiveOrder(
            client_order_id="cancel-unknown", intent_id="intent-1", symbol="ALTUSDT",
            side="SELL", order_type="LIMIT", execution_policy="maker_first",
            state=OrderState.ACKED, quantity=D("0.1"), created_time=1, updated_time=1,
        )

        async def unknown_cancel(_symbol, _client_id):
            raise UnknownExecutionStatus("cancel timeout")

        gateway.trade_ws.cancel_order = unknown_cancel
        confirmed = asyncio.run(manager._cancel_if_open(order))
        self.assertFalse(confirmed)
        self.assertTrue(manager.safe_halt_reason.startswith("cancel_status_unknown:"))

    def test_safe_halt_blocks_new_exposure_but_allows_reduce_only_exit(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        manager.safe_halt("manual_test_halt")
        result = asyncio.run(
            manager.handle_intent(intent(IntentAction.CLOSE_SHORT, "exit"), self.quote)
        )
        self.assertEqual(result["status"], "closed")
        self.assertEqual([row["side"] for row in gateway.trade_ws.order_calls], ["SELL", "BUY"])
        rejected = asyncio.run(
            manager.handle_intent(intent(IntentAction.OPEN_SHORT, "blocked"), self.quote)
        )
        self.assertEqual(rejected["status"], "rejected")

    def test_safe_halt_preserves_new_reconcile_reason_during_stream_failure(self) -> None:
        manager, _gateway, ledger = self.manager()
        manager.safe_halt("private_stream_failed:RuntimeError")
        manager.safe_halt("position_quantity_mismatch:ALTUSDT")
        manager.safe_halt("position_quantity_mismatch:ALTUSDT")
        self.assertEqual(
            manager.safe_halt_reason,
            "private_stream_failed:RuntimeError | position_quantity_mismatch:ALTUSDT",
        )
        self.assertEqual(ledger.get_meta("safe_halt_reason"), manager.safe_halt_reason)
        recovered = LiveOrderManager(
            manager.config, FakeGateway(), ledger, self.rules, orders_authorized=True,
        )
        self.assertEqual(recovered.safe_halt_reason, manager.safe_halt_reason)

    def test_private_stream_recovery_clears_only_recoverable_halts(self) -> None:
        manager, _gateway, ledger = self.manager()
        manager.safe_halt("listenkeyexpired")
        manager.safe_halt("private_stream_failed:RuntimeError")
        manager.safe_halt("position_quantity_mismatch:ALTUSDT")

        cleared = manager.clear_safe_halt_reasons({
            "listenkeyexpired", "private_stream_failed:RuntimeError",
        })

        self.assertEqual(
            cleared,
            ["listenkeyexpired", "private_stream_failed:RuntimeError"],
        )
        self.assertEqual(manager.safe_halt_reason, "position_quantity_mismatch:ALTUSDT")
        self.assertEqual(ledger.get_meta("safe_halt_reason"), manager.safe_halt_reason)

    def test_successful_reconcile_only_recovers_connectivity_halts(self) -> None:
        reasons = (
            "reconcile_failed_3x:GatewayError:code=None:status=None | "
            "private_stream_failed:RuntimeError | "
            "position_quantity_mismatch:ALTUSDT"
        )
        self.assertEqual(
            recoverable_connectivity_halts(reasons),
            {
                "reconcile_failed_3x:GatewayError:code=None:status=None",
                "private_stream_failed:RuntimeError",
            },
        )
        self.assertEqual(
            recoverable_connectivity_halts(
                reasons, private_stream_confirmed=False,
            ),
            {"reconcile_failed_3x:GatewayError:code=None:status=None"},
        )

    def test_optional_reconcile_failures_do_not_discard_critical_snapshot(self) -> None:
        class Rest:
            def sync_time(self):
                raise GatewayError("time unavailable", endpoint="/fapi/v1/time")

            def reconcile_snapshot(self):
                return {
                    "account": {"totalMarginBalance": "100"},
                    "positions": [],
                    "open_orders": [],
                    "open_algo_orders": [],
                }

            def income(self, *_args):
                raise GatewayError("income unavailable", endpoint="/papi/v1/um/income")

        inputs = asyncio.run(fetch_reconcile_inputs(Rest(), 0))
        self.assertEqual(inputs.account["totalMarginBalance"], "100")
        self.assertEqual(inputs.positions, [])
        self.assertEqual(set(inputs.optional_errors), {"time_sync", "income"})

    def test_authoritative_reconcile_auto_recovers_without_manual_edit(self) -> None:
        manager, _gateway, ledger = self.manager()
        reason = "reconcile_failed_3x:/papi/v1/account:GatewayError:code=None:status=None"
        manager.safe_halt(reason)
        notices: list[list[str]] = []
        service = ClaudeLiveTradingService.__new__(ClaudeLiveTradingService)
        service.runtime = SimpleNamespace(oms=manager, ledger=ledger)
        service.notifier = SimpleNamespace(
            recovered=lambda cleared: notices.append(list(cleared)),
        )

        cleared = asyncio.run(
            service._clear_recovered_connectivity_halts(
                "test_reconcile", private_stream_confirmed=False,
            )
        )

        self.assertEqual(cleared, [reason])
        self.assertEqual(manager.safe_halt_reason, "")
        self.assertEqual(notices, [[reason]])
        with ledger.connection() as conn:
            event = conn.execute(
                "SELECT event_type FROM live_events ORDER BY event_time DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event[0], "CONNECTIVITY_AUTO_RECOVERED")

    def test_portfolio_risk_level_change_halts_new_exposure(self) -> None:
        manager, _gateway, ledger = self.manager()
        asyncio.run(manager.handle_user_event({
            "e": "riskLevelChange",
            "E": 1_700_000_000_000,
            "s": "MARGIN_CALL",
            "u": "1.2",
        }))
        self.assertEqual(manager.safe_halt_reason, "risk_level_change:margin_call")
        self.assertEqual(ledger.get_meta("safe_halt_reason"), manager.safe_halt_reason)

    def test_closed_position_unresolved_algo_cancel_halts_reentry(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        gateway.trade_ws.algo_cancel_result = {"algoStatus": "NEW"}
        gateway.rest.algo_query_result = {"algoStatus": "NEW"}
        closed = asyncio.run(
            manager.handle_intent(intent(IntentAction.CLOSE_SHORT, "exit-cancel"), self.quote)
        )
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(
            manager.safe_halt_reason.startswith("closed_position_algo_cancel_unresolved:")
        )

    def test_explicit_exit_rejection_restores_open_position_and_halts(self) -> None:
        manager, gateway, ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))

        async def reject_exit(_params):
            raise GatewayError("rejected", code=-2010, status=400)

        gateway.trade_ws.place_order = reject_exit
        result = asyncio.run(
            manager.handle_intent(intent(IntentAction.CLOSE_SHORT, "exit-reject"), self.quote)
        )
        self.assertEqual(result["status"], "open")
        self.assertEqual(manager.positions_by_symbol["ALTUSDT"].status, "open")
        self.assertTrue(manager.safe_halt_reason.startswith("exit_order_rejected:"))
        stored = ledger.order(result["order"]["client_order_id"])
        self.assertEqual(stored["state"], "EXCHANGE_REJECTED")

    def test_exchange_flat_reconcile_recovers_delayed_algo_exit_fill(self) -> None:
        manager, gateway, ledger = self.manager()
        opened = asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions_by_symbol["ALTUSDT"]
        gateway.rest.trades = [{
            "orderId": 7001,
            "id": 8001,
            "symbol": "ALTUSDT",
            "side": "BUY",
            "positionSide": "SHORT",
            "qty": str(position.quantity),
            "price": "95",
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "0.75",
            "maker": False,
            "time": position.entry_time + 60_000,
        }]
        asyncio.run(manager.reconcile({
            "positions": [], "open_orders": [], "open_algo_orders": [],
        }))
        with ledger.connection() as conn:
            row = dict(conn.execute(
                "SELECT status,exit_price,realized_pnl FROM live_positions WHERE position_id=?",
                (opened["position"]["position_id"],),
            ).fetchone())
        self.assertEqual(row["status"], "closed")
        self.assertEqual(D(row["exit_price"]), D("95"))
        self.assertEqual(D(row["realized_pnl"]), D("0.75"))
        self.assertFalse(manager.safe_halt_reason)

    def test_private_trade_update_applies_incremental_entry_fill(self) -> None:
        manager, _gateway, ledger = self.manager()
        stored_intent = intent()
        ledger.save_intent(stored_intent)
        order = LiveOrder(
            client_order_id="stream-entry", intent_id=stored_intent.intent_id,
            symbol="ALTUSDT", side="SELL", order_type="LIMIT",
            execution_policy="maker_first", state=OrderState.ACKED,
            quantity=D("0.2"), created_time=1, updated_time=1,
        )
        manager.orders[order.client_order_id] = order
        ledger.save_order(order)
        asyncio.run(manager.handle_user_event({
            "e": "ORDER_TRADE_UPDATE", "E": 10,
            "o": {
                "c": "stream-entry", "i": 99, "z": "0.1", "ap": "100",
                "X": "PARTIALLY_FILLED", "x": "TRADE", "t": 1,
                "s": "ALTUSDT", "S": "SELL", "l": "0.1", "L": "100",
                "n": "0", "N": "USDT", "rp": "0", "m": False, "T": 10,
            },
        }))
        self.assertEqual(manager.positions["position-1"].quantity, D("0.1"))
        self.assertEqual(order.applied_quantity, D("0.1"))

    def test_signal_conversion_keeps_strategy_stop_and_exit_reason(self) -> None:
        signal = WaterfallSignal(
            signal_id="sig", position_id="pos", symbol="ALTUSDT", strategy="claude_board_wf_1m",
            action="take_profit", family="board_waterfall", rule="board40_drop7_60m",
            decision_time=123, price=90.0, stop_price=102.0,
            evidence=["exit_reason=take_profit_trailing"],
        )
        converted = signal_to_intent(signal)
        self.assertEqual(converted.action, IntentAction.CLOSE_SHORT)
        self.assertEqual(converted.reason, "take_profit_trailing")
        self.assertEqual(converted.strategy_stop_price, D("102.0"))

    def test_restart_recovers_filled_but_unapplied_entry_once(self) -> None:
        manager, gateway, ledger = self.manager()
        stored_intent = intent()
        ledger.save_intent(stored_intent)
        order = LiveOrder(
            client_order_id="crash-entry", intent_id=stored_intent.intent_id, symbol="ALTUSDT",
            side="SELL", order_type="MARKET", execution_policy="market", state=OrderState.FILLED,
            quantity=D("0.1"), filled_quantity=D("0.1"), applied_quantity=D("0"),
            average_price=D("100"), exchange_order_id=77, created_time=1, updated_time=1,
        )
        ledger.save_order(order)
        recovered = LiveOrderManager(manager.config, gateway, ledger, self.rules, orders_authorized=True)
        recovered.set_account(self.account)
        gateway.rest.query_result = {
            "orderId": 77, "status": "FILLED", "executedQty": "0.1", "avgPrice": "100",
        }
        asyncio.run(recovered.recover_inflight_orders())
        self.assertEqual(recovered.positions["position-1"].quantity, D("0.1"))
        asyncio.run(recovered.recover_inflight_orders())
        self.assertEqual(recovered.positions["position-1"].quantity, D("0.1"))

    def test_reconcile_exchange_flat_closes_local_without_duplicate_exit(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions_by_symbol["ALTUSDT"]
        asyncio.run(manager.update_trail(position.position_id, D("98"), True, 10))
        active_algos = {
            position.structure_client_algo_id,
            position.trail_client_algo_id,
        }
        gateway.rest.trades = [{
            "orderId": 7002, "id": 8002, "side": "BUY", "positionSide": "SHORT",
            "qty": str(position.quantity), "price": "99", "realizedPnl": "0.1",
            "commission": "0", "commissionAsset": "USDT", "maker": False,
            "time": position.entry_time + 60_000,
        }]
        result = asyncio.run(manager.reconcile({
            "positions": [],
            "open_orders": [],
            "open_algo_orders": [
                {"clientAlgoId": client_id, "symbol": "ALTUSDT", "algoStatus": "NEW"}
                for client_id in active_algos
            ],
        }))
        self.assertTrue(result["ok"])
        self.assertNotIn("ALTUSDT", manager.positions_by_symbol)
        self.assertEqual(set(gateway.trade_ws.algo_cancel_calls), active_algos)

    def test_reconcile_halts_on_external_order_and_cancels_owned_orphan(self) -> None:
        manager, gateway, _ledger = self.manager()
        result = asyncio.run(manager.reconcile({
            "positions": [],
            "open_algo_orders": [],
            "open_orders": [
                {"symbol": "ALTUSDT", "clientOrderId": "manual-order", "side": "SELL"},
                {"symbol": "ALTUSDT", "clientOrderId": "bh-en-orphan", "side": "SELL"},
            ],
        }))
        self.assertFalse(result["ok"])
        self.assertTrue(manager.safe_halt_reason.startswith("external_open_orders:"))
        self.assertEqual(gateway.trade_ws.order_cancel_calls, [("ALTUSDT", "bh-en-orphan")])

    def test_income_excludes_transfers_from_daily_loss(self) -> None:
        path = self.root / "income.db"
        ledger = LiveLedger(path)
        ledger.save_income([
            {"tranId": 1, "incomeType": "TRANSFER", "asset": "USDT", "income": "-50", "time": 100},
            {"tranId": 2, "incomeType": "REALIZED_PNL", "asset": "USDT", "income": "-1.5", "time": 101},
            {"tranId": 3, "incomeType": "COMMISSION", "asset": "USDT", "income": "-0.1", "time": 102},
            {"tranId": 4, "incomeType": "FUNDING_FEE", "asset": "USDT", "income": "0.2", "time": 103},
        ])
        self.assertEqual(ledger.trading_income_since(0), D("-1.4"))

    def test_realized_drawdown_ladder_matches_paper_sizing(self) -> None:
        cfg = ladder_live_config(self.root)
        ledger = LiveLedger(cfg.ledger_path)
        manager = LiveOrderManager(
            cfg, FakeGateway(), ledger, self.rules, orders_authorized=True,
        )
        manager.set_account(self.account)
        decision = manager.risk.evaluate_short_entry(
            intent(), self.quote, self.rules.get("ALTUSDT"), self.account, 0,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, D("100.000"))
        self.assertEqual(decision.margin_fraction, D("0.10"))
        self.assertEqual(decision.sizing_factor, D("1.0"))

        start = manager.sizing_start_time
        ledger.save_income([
            {"tranId": 100, "incomeType": "TRANSFER", "asset": "USDT", "income": "-50", "time": start + 1},
            {"tranId": 101, "incomeType": "REALIZED_PNL", "asset": "USDT", "income": "-6", "time": start + 2},
        ])
        state = manager.refresh_sizing_state(initialize=True)
        self.assertEqual(state["equity"], D("94"))
        self.assertEqual(state["drawdown_pct"], D("0.06"))
        self.assertEqual(state["factor"], D("0.75"))
        decision = manager.risk.evaluate_short_entry(
            intent(suffix="ladder-75"), self.quote,
            self.rules.get("ALTUSDT"), self.account, 0,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, D("70.5000"))
        self.assertEqual(decision.margin_fraction, D("0.0750"))

        ledger.save_income([
            {"tranId": 102, "incomeType": "REALIZED_PNL", "asset": "USDT", "income": "-5", "time": start + 3},
        ])
        state = manager.refresh_sizing_state(initialize=True)
        self.assertEqual(state["equity"], D("89"))
        self.assertEqual(state["factor"], D("0.5"))
        decision = manager.risk.evaluate_short_entry(
            intent(suffix="ladder-50"), self.quote,
            self.rules.get("ALTUSDT"), self.account, 0,
        )
        self.assertEqual(decision.notional, D("44.5000"))

        ledger.save_income([
            {"tranId": 103, "incomeType": "COMMISSION", "asset": "USDT", "income": "-5", "time": start + 4},
        ])
        state = manager.refresh_sizing_state(initialize=True)
        self.assertEqual(state["equity"], D("84"))
        self.assertEqual(state["factor"], D("0.25"))
        decision = manager.risk.evaluate_short_entry(
            intent(suffix="ladder-25"), self.quote,
            self.rules.get("ALTUSDT"), self.account, 0,
        )
        self.assertEqual(decision.notional, D("21.0000"))

    def test_drawdown_sizing_baseline_and_peak_survive_restart(self) -> None:
        cfg = ladder_live_config(self.root)
        ledger = LiveLedger(cfg.ledger_path)
        first = LiveOrderManager(
            cfg, FakeGateway(), ledger, self.rules, orders_authorized=True,
        )
        first.set_account(self.account)
        start = first.sizing_start_time
        ledger.save_income([
            {"tranId": 201, "incomeType": "REALIZED_PNL", "asset": "USDT", "income": "10", "time": start + 1},
            {"tranId": 202, "incomeType": "TRANSFER", "asset": "USDT", "income": "-40", "time": start + 2},
        ])
        first.refresh_sizing_state(initialize=True)
        self.assertEqual(first.risk.sizing_equity, D("110"))
        self.assertEqual(first.risk.sizing_peak_equity, D("110"))

        ledger.save_income([
            {"tranId": 203, "incomeType": "REALIZED_PNL", "asset": "USDT", "income": "-8", "time": start + 3},
        ])
        restarted = LiveOrderManager(
            cfg, FakeGateway(), LiveLedger(cfg.ledger_path), self.rules,
            orders_authorized=True,
        )
        restarted.set_account(AccountSnapshot(10, D("62"), D("62"), D("62"), D("0"), D("0")))
        self.assertEqual(restarted.sizing_start_time, start)
        self.assertEqual(restarted.risk.sizing_equity, D("102"))
        self.assertEqual(restarted.risk.sizing_peak_equity, D("110"))
        self.assertAlmostEqual(float(restarted.risk.sizing_drawdown_pct), 8 / 110, places=9)
        self.assertEqual(restarted.risk.sizing_factor, D("0.75"))

    def test_added_fill_replaces_structure_stop_new_before_old_cancel(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions["position-1"]
        old_client = position.structure_client_algo_id
        extra = LiveOrder(
            client_order_id="extra-entry", intent_id="intent-1", symbol="ALTUSDT",
            side="SELL", order_type="MARKET", execution_policy="market", state=OrderState.FILLED,
            quantity=D("0.05"), filled_quantity=D("0.05"), average_price=D("110"),
        )
        asyncio.run(manager._apply_entry_fill(intent(), extra, self.rules.get("ALTUSDT")))
        self.assertTrue(position.protected)
        self.assertNotEqual(position.structure_client_algo_id, old_client)
        self.assertEqual(gateway.trade_ws.algo_cancel_calls, [old_client])

    def test_order_execution_metrics_are_persisted_and_reported(self) -> None:
        manager, _gateway, ledger = self.manager()
        measured_intent = TradeIntent(
            intent_id="metrics-intent", signal_id="metrics-signal", position_id="metrics-position",
            strategy="claude_board_wf_1m", symbol="ALTUSDT",
            action=IntentAction.OPEN_SHORT, decision_time=900,
            signal_price=D("100"), strategy_stop_price=D("102"), reason="strategy_entry",
        )
        ledger.save_intent(measured_intent)
        order = LiveOrder(
            client_order_id="metrics-order", intent_id=measured_intent.intent_id,
            symbol="ALTUSDT", side="SELL", order_type="MARKET", execution_policy="market",
            state=OrderState.SUBMITTING, quantity=D("1"), reference_price=D("100"),
            arrival_price=D("99.5"),
            created_time=950, submit_time=1000, updated_time=1000,
        )
        manager._apply_order_result(order, {
            "orderId": 17, "status": "FILLED", "executedQty": "1",
            "avgPrice": "99", "updateTime": 1200,
        })
        row = ledger.dashboard_snapshot(limit=1)["orders"][0]
        self.assertEqual(D(row["slippage_bps"]), D("100"))
        self.assertAlmostEqual(float(row["arrival_slippage_bps"]), 50.251256, places=5)
        self.assertEqual(row["first_fill_time"], 1200)
        self.assertEqual(row["final_fill_time"], 1200)
        self.assertEqual(row["signal_to_submit_ms"], 100)
        self.assertEqual(row["signal_to_fill_ms"], 300)
        self.assertEqual(row["signal_to_final_fill_ms"], 300)
        self.assertGreaterEqual(row["submit_to_ack_ms"], 0)

    def test_live_notification_contains_fill_latency_and_slippage(self) -> None:
        cfg = live_config(self.root)
        notifier = LiveEventNotifier({"live_trading": {"notify_wecom": True}}, cfg)
        sent: list[str] = []
        notifier._send = lambda content: (sent.append(content) or True, "")
        measured = intent()
        notifier.intent_result(measured, {
            "status": "filled",
            "order": {
                "average_price": "99.8", "filled_quantity": "0.2",
                "first_fill_time": measured.decision_time + 240,
                "slippage_bps": "20.0",
            },
            "position": {"protected": True},
        })
        self.assertIn("延迟 240ms", sent[0])
        self.assertIn("滑点 20.0bp", sent[0])
        self.assertIn("已保护", sent[0])

    def test_buy_slippage_is_adverse_when_fill_is_above_reference(self) -> None:
        manager, _gateway, _ledger = self.manager()
        order = LiveOrder(
            client_order_id="buy-metrics", intent_id="exit", symbol="ALTUSDT",
            side="BUY", order_type="MARKET", execution_policy="market",
            state=OrderState.SUBMITTING, quantity=D("1"), reference_price=D("100"),
            submit_time=1000,
        )
        manager._apply_order_result(order, {
            "orderId": 18, "status": "FILLED", "executedQty": "1",
            "avgPrice": "101", "updateTime": 1200,
        })
        self.assertEqual(order.slippage_bps, D("100"))

    def test_excessive_actual_entry_slippage_is_flattened_and_halted(self) -> None:
        manager, gateway, ledger = self.manager()

        async def adverse_fill(params):
            gateway.trade_ws.order_calls.append(dict(params))
            return {
                "orderId": len(gateway.trade_ws.order_calls),
                "status": "FILLED",
                "executedQty": str(params.get("quantity") or "0"),
                "avgPrice": "99" if params["side"] == "SELL" else "99.1",
            }

        gateway.trade_ws.place_order = adverse_fill
        result = asyncio.run(manager.handle_intent(intent(), self.quote))
        self.assertIsNone(result["position"])
        self.assertNotIn("ALTUSDT", manager.positions_by_symbol)
        self.assertEqual([row["side"] for row in gateway.trade_ws.order_calls], ["SELL", "BUY"])
        self.assertIn("entry_slippage_exceeded", manager.safe_halt_reason)
        with ledger.connection() as conn:
            event = conn.execute(
                "SELECT 1 FROM live_events WHERE event_type='ENTRY_SLIPPAGE_LIMIT_EXCEEDED'"
            ).fetchone()
        self.assertIsNotNone(event)

    def test_unknown_trail_replacement_keeps_old_protection_and_halts(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions["position-1"]
        asyncio.run(manager.update_trail(position.position_id, D("98"), True, 10))
        old_id = position.trail_client_algo_id
        old_price = position.trail_price

        async def unknown_algo(_params):
            raise UnknownExecutionStatus("timeout")

        def unknown_query(*_args, **_kwargs):
            raise GatewayError("not found", code=-2013, status=400)

        gateway.trade_ws.place_algo = unknown_algo
        gateway.rest.query_algo_order = unknown_query
        asyncio.run(manager.update_trail(position.position_id, D("97"), True, 20))
        self.assertEqual(position.trail_client_algo_id, old_id)
        self.assertEqual(position.trail_price, old_price)
        self.assertIn("trail_replace_unresolved", manager.safe_halt_reason)

    def test_order_ack_uses_binance_clock_offset(self) -> None:
        manager, gateway, _ledger = self.manager()
        gateway.rest.time_offset_ms = 2_000
        order = LiveOrder(
            client_order_id="clock-test", intent_id="clock-intent", symbol="ALTUSDT",
            side="SELL", order_type="MARKET", execution_policy="market",
            state=OrderState.SUBMITTING, quantity=D("0.1"), created_time=1, updated_time=1,
        )
        expected = int(time.time() * 1000) + 2_000
        manager._apply_order_result(order, {
            "orderId": 123, "status": "NEW", "executedQty": "0", "avgPrice": "0",
        })
        self.assertLess(abs(order.ack_time - expected), 250)

    def test_partial_close_resizes_exchange_protection_to_remaining_quantity(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions["position-1"]
        original = position.quantity
        old_stop = position.structure_client_algo_id
        close_order = LiveOrder(
            client_order_id="partial-resize", intent_id="exit-resize", symbol="ALTUSDT",
            side="BUY", order_type="LIMIT", execution_policy="ioc",
            state=OrderState.PARTIALLY_FILLED, quantity=original,
            filled_quantity=original / D("2"), average_price=D("99"), reduce_only=True,
        )
        asyncio.run(manager._apply_exit_fill(position, close_order))
        self.assertEqual(position.quantity, original / D("2"))
        self.assertEqual(D(gateway.trade_ws.algo_calls[-1]["quantity"]), position.quantity)
        self.assertIn(old_stop, gateway.trade_ws.algo_cancel_calls)

    def test_reconcile_uses_exchange_quantity_and_replaces_stale_stop(self) -> None:
        manager, gateway, _ledger = self.manager()
        asyncio.run(manager.handle_intent(intent(), self.quote))
        position = manager.positions["position-1"]
        original = position.quantity
        remaining = original / D("2")
        old_stop = position.structure_client_algo_id
        result = asyncio.run(manager.reconcile({
            "positions": [{
                "symbol": "ALTUSDT", "positionSide": "SHORT",
                "positionAmt": str(-remaining), "entryPrice": "100", "liquidationPrice": "150",
            }],
            "open_orders": [],
            "open_algo_orders": [{
                "clientAlgoId": old_stop, "symbol": "ALTUSDT", "side": "BUY",
                "quantity": str(original), "algoStatus": "NEW",
            }],
        }))
        self.assertFalse(result["ok"])
        self.assertEqual(position.quantity, remaining)
        self.assertEqual(D(gateway.trade_ws.algo_calls[-1]["quantity"]), remaining)
        self.assertIn(old_stop, gateway.trade_ws.algo_cancel_calls)

    def test_failed_exit_then_same_bar_entry_cannot_replace_old_strategy_position(self) -> None:
        service = ClaudeLiveTradingService.__new__(ClaudeLiveTradingService)
        saved: list[dict] = []
        old_strategy = SimpleNamespace(
            position_id="old-position", status="closed", exit_time=1, exit_price=99.0,
            pnl_pct=0.02, pnl_usdt=2.0, exit_reason="take_profit_trailing", updated_time=1,
        )
        old_strategy.to_dict = lambda: {"position_id": old_strategy.position_id}
        new_strategy = SimpleNamespace(
            position_id="new-position", status="open", exit_time=None, exit_price=0.0,
            pnl_pct=0.0, pnl_usdt=0.0, exit_reason="", updated_time=1,
        )
        new_strategy.to_dict = lambda: {"position_id": new_strategy.position_id}
        live_position = SimpleNamespace(
            position_id="old-position", entry_price=D("100"), structure_stop_price=D("102"),
            quantity=D("0.1"),
        )
        service.runtime = SimpleNamespace(
            config=SimpleNamespace(sends_real_orders=True, leverage=3),
            oms=SimpleNamespace(
                orders_authorized=True, positions_by_symbol={"ALTUSDT": live_position}, orders={},
            ),
        )
        service.engine = SimpleNamespace(
            positions={"ALTUSDT": old_strategy}, realized_pnl_usdt=2.0,
        )
        service.strategy_store = SimpleNamespace(upsert_waterfall_position=lambda row: saved.append(row))
        exit_signal = WaterfallSignal(
            signal_id="exit", position_id="old-position", symbol="ALTUSDT",
            strategy="claude_board_wf_1m", action="take_profit", family="board_waterfall",
            rule="board40_drop7_60m", decision_time=1, price=99.0, stop_price=102.0,
        )
        service._sync_execution_outcome(exit_signal, old_strategy, {"status": "unknown"})
        self.assertEqual(service.engine.positions["ALTUSDT"].position_id, "old-position")
        self.assertEqual(service.engine.realized_pnl_usdt, 0.0)
        entry_signal = WaterfallSignal(
            signal_id="entry", position_id="new-position", symbol="ALTUSDT",
            strategy="claude_board_wf_1m", action="open_short", family="board_waterfall",
            rule="board40_drop7_60m", decision_time=1, price=99.0, stop_price=102.0,
        )
        service._sync_execution_outcome(
            entry_signal, new_strategy, {"status": "rejected", "reason": "position_exists"},
        )
        self.assertEqual(service.engine.positions["ALTUSDT"].position_id, "old-position")
        self.assertEqual(new_strategy.status, "execution_rejected")

    def test_periodic_reconcile_syncs_late_entry_fill_into_strategy_state(self) -> None:
        service = ClaudeLiveTradingService.__new__(ClaudeLiveTradingService)
        saved: list[dict] = []
        strategy_position = SimpleNamespace(
            position_id="position-1", status="open", entry_price=100.0,
            best_price=99.0, worst_price=101.0, stop_price=102.0,
            notional_usdt=20.0, margin_usdt=2.0, leverage=10.0,
            evidence=[], updated_time=1,
        )
        strategy_position.to_dict = lambda: {
            "position_id": strategy_position.position_id,
            "entry_price": strategy_position.entry_price,
        }
        live_position = SimpleNamespace(
            position_id="position-1", entry_price=D("98"),
            structure_stop_price=D("103"), quantity=D("0.25"),
        )
        service.runtime = SimpleNamespace(
            config=SimpleNamespace(sends_real_orders=True, leverage=10),
            oms=SimpleNamespace(
                orders_authorized=True,
                positions_by_symbol={"ALTUSDT": live_position},
                safe_halt=lambda _reason: None,
            ),
        )
        service.engine = SimpleNamespace(positions={"ALTUSDT": strategy_position})
        service.strategy_store = SimpleNamespace(
            upsert_waterfall_position=lambda row: saved.append(row),
        )
        service._reconcile_strategy_positions_with_live("test")
        self.assertEqual(strategy_position.entry_price, 98.0)
        self.assertEqual(strategy_position.best_price, 98.0)
        self.assertEqual(strategy_position.worst_price, 101.0)
        self.assertEqual(strategy_position.stop_price, 103.0)
        self.assertEqual(strategy_position.notional_usdt, 24.5)
        self.assertEqual(strategy_position.margin_usdt, 2.45)
        self.assertIn("live_execution_synced", strategy_position.evidence)
        self.assertTrue(saved)

    def test_reconcile_heartbeat_does_not_reset_processed_event_count(self) -> None:
        service = ClaudeLiveTradingService.__new__(ClaudeLiveTradingService)
        meta: dict[str, str] = {}
        service.runtime = SimpleNamespace(
            ledger=SimpleNamespace(
                set_meta=lambda key, value, _stamp: meta.__setitem__(key, value),
            ),
        )
        service._last_heartbeat_ms = 0
        service._processed_events = 0
        service._heartbeat("running", 17, force=True)
        service._heartbeat("running", force=True)
        self.assertEqual(meta["service_processed_events"], "17")

    def test_heartbeat_event_count_never_moves_backwards(self) -> None:
        service = ClaudeLiveTradingService.__new__(ClaudeLiveTradingService)
        meta: dict[str, str] = {}
        service.runtime = SimpleNamespace(
            ledger=SimpleNamespace(
                set_meta=lambda key, value, _stamp: meta.__setitem__(key, value),
            ),
        )
        service._last_heartbeat_ms = 0
        service._processed_events = 0
        service._heartbeat("running", 17, force=True)
        service._heartbeat("running", 12, force=True)
        self.assertEqual(meta["service_processed_events"], "17")


if __name__ == "__main__":
    unittest.main()
