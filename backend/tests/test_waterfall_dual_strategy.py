from __future__ import annotations

import unittest
from unittest.mock import patch

from pump_dump_hunter.board_waterfall import BoardWaterfallEngine, STRATEGY_NAME
from pump_dump_hunter.data.store import Store
from pump_dump_hunter.models import Candle, KlineClosed
from pump_dump_hunter.waterfall import WaterfallEngine, WaterfallPosition, prewarm_waterfall_symbols, refresh_waterfall_universe
from pump_dump_hunter.web import combine_waterfall_accounts

from .helpers import temp_settings


def position_row(position_id: str, strategy: str, status: str = "closed", pnl_usdt: float = 0.0) -> dict:
    return WaterfallPosition(
        position_id=position_id,
        symbol=f"{position_id.upper()}USDT",
        strategy=strategy,
        family="board_waterfall" if strategy == STRATEGY_NAME else "post_pump",
        rule="test_rule",
        exit_profile="claude_e1" if strategy == STRATEGY_NAME else "medium_30_lock",
        status=status,
        side="SHORT",
        entry_time=1_700_000_000_000,
        entry_price=100.0,
        notional_usdt=200.0,
        stop_price=103.0,
        best_price=95.0,
        worst_price=101.0,
        trail_price=0.0,
        exit_time=1_700_000_060_000 if status == "closed" else None,
        exit_price=95.0 if status == "closed" else 0.0,
        pnl_pct=0.0492 if status == "closed" else 0.0,
        pnl_usdt=pnl_usdt,
        exit_reason="take_profit_trailing" if status == "closed" else "",
        margin_usdt=20.0,
        leverage=10.0,
        capital_fraction=0.2,
        updated_time=1_700_000_060_000,
    ).to_dict()


class StoreStrategyIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = temp_settings()
        self.store = Store(self.settings["paths"]["db_path"])
        self.core = WaterfallEngine(self.settings)

    def test_strategy_filter_and_unlimited_position_history(self) -> None:
        core_closed = position_row("core-closed", self.core.strategy, pnl_usdt=9.0)
        core_open = position_row("core-open", self.core.strategy, status="open")
        board_open = position_row("board-open", STRATEGY_NAME, status="open")
        for row in (core_closed, core_open, board_open):
            self.store.upsert_waterfall_position(row)

        self.assertEqual(len(self.store.waterfall_position_rows(limit=1, strategy=self.core.strategy)), 1)
        self.assertEqual(len(self.store.waterfall_position_rows(limit=0, strategy=self.core.strategy)), 2)
        active = self.store.active_waterfall_positions(strategy=self.core.strategy)
        self.assertEqual([row["position_id"] for row in active], ["core-open"])

    def test_core_restart_ignores_board_positions_and_pnl(self) -> None:
        core_closed = position_row("core-closed", self.core.strategy, pnl_usdt=9.0)
        board_closed = position_row("board-closed", STRATEGY_NAME, pnl_usdt=50.0)
        board_open = position_row("board-open", STRATEGY_NAME, status="open")
        self.core.load_positions([board_open])
        self.core.load_recent_state([core_closed, board_closed], [])

        self.assertEqual(self.core.positions, {})
        self.assertEqual(self.core.realized_pnl_usdt, 9.0)
        self.assertNotIn(board_open["symbol"], self.core.last_signal_time)


class WaterfallRuntimeRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = temp_settings()

    def test_combined_summary_sums_independent_initial_balances(self) -> None:
        accounts = [
            {
                "open_positions": 1,
                "closed_positions": 2,
                "signals": 4,
                "paper_initial_balance_usdt": 100.0,
                "paper_realized_pnl_usdt": 10.0,
                "paper_unrealized_pnl_usdt": 0.0,
                "paper_equity_usdt": 110.0,
                "paper_used_margin_usdt": 20.0,
                "paper_free_balance_usdt": 90.0,
                "win_rate": 0.5,
                "avg_pnl_pct": 0.02,
            },
            {
                "open_positions": 0,
                "closed_positions": 1,
                "signals": 2,
                "paper_initial_balance_usdt": 100.0,
                "paper_realized_pnl_usdt": 10.0,
                "paper_unrealized_pnl_usdt": 0.0,
                "paper_equity_usdt": 110.0,
                "paper_used_margin_usdt": 0.0,
                "paper_free_balance_usdt": 110.0,
                "win_rate": 1.0,
                "avg_pnl_pct": 0.05,
            },
        ]
        out = combine_waterfall_accounts({"watch": 8}, accounts)
        self.assertEqual(out["paper_initial_balance_usdt"], 200.0)
        self.assertEqual(out["paper_equity_usdt"], 220.0)
        self.assertEqual(out["closed_positions"], 3.0)
        self.assertAlmostEqual(out["win_rate"], 2.0 / 3.0)
        self.assertAlmostEqual(out["avg_pnl_pct"], 0.03)

    def test_board_engine_does_not_size_beyond_free_equity(self) -> None:
        engine = BoardWaterfallEngine(self.settings)
        engine.realized_pnl_usdt = -engine.initial_balance_usdt
        sizing = engine.paper_sizing()
        self.assertEqual(sizing["equity_usdt"], 0.0)
        self.assertEqual(sizing["margin_usdt"], 0.0)
        self.assertEqual(sizing["notional_usdt"], 0.0)

    def test_board_engine_entry_and_trailing_exit_lifecycle(self) -> None:
        engine = BoardWaterfallEngine(self.settings)
        start = 1_700_000_000_000
        history: list[Candle] = []
        for i in range(1381):
            history.append(self._candle("BOARDUSDT", start + i * 60_000, 100.0, 100.0, 10_000.0))
        for i in range(59):
            price = 100.0 + 60.0 * (i + 1) / 59.0
            history.append(self._candle("BOARDUSDT", start + (1381 + i) * 60_000, price, price, 10_000.0))
        engine.prime_candles(history)

        trigger = self._candle("BOARDUSDT", start + 1440 * 60_000, 160.0, 148.0, 20_000.0, high=160.0, low=147.0)
        _, changed, signals = engine.on_kline(KlineClosed("BOARDUSDT", "1m", trigger))
        self.assertEqual([signal.action for signal in signals], ["open_short"])
        self.assertEqual(changed[0].exit_profile, "claude_e1")

        continuation = self._candle("BOARDUSDT", start + 1441 * 60_000, 148.0, 141.0, 20_000.0, high=148.0, low=140.0)
        _, _, signals = engine.on_kline(KlineClosed("BOARDUSDT", "1m", continuation))
        self.assertEqual(signals, [])
        self.assertGreater(engine.positions["BOARDUSDT"].trail_price, 0.0)

        rebound = self._candle("BOARDUSDT", start + 1442 * 60_000, 141.0, 145.0, 10_000.0, high=145.0, low=140.5)
        _, changed, signals = engine.on_kline(KlineClosed("BOARDUSDT", "1m", rebound))
        self.assertEqual([signal.action for signal in signals], ["take_profit"])
        self.assertEqual(changed[0].status, "closed")
        self.assertNotIn("BOARDUSDT", engine.positions)

    def test_db_only_prewarm_never_calls_rest(self) -> None:
        engine = WaterfallEngine(self.settings)
        now = 1_700_000_000_000
        cached = Candle(
            symbol="ALTUSDT",
            interval="1m",
            open_time=now,
            open=1.0,
            high=1.01,
            low=0.99,
            close=1.0,
            volume=100.0,
            close_time=now + 59_999,
            quote_volume=1000.0,
            trades=10,
        )

        class FakeStore:
            def load_candles(self, *_args, **_kwargs):
                return [cached]

            def save_candles(self, _rows):
                return None

            def upsert_waterfall_watch(self, _rows):
                return None

        class FakeClient:
            calls = 0

            def klines(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("REST must not be called in DB-only mode")

        client = FakeClient()
        prewarm_waterfall_symbols(client, FakeStore(), engine, ["ALTUSDT"], 1500, 1, allow_rest=False)
        self.assertEqual(client.calls, 0)

    def test_rest_universe_failure_uses_db_only_prewarm(self) -> None:
        engine = WaterfallEngine(self.settings)

        class FakeStore:
            def candle_symbols(self, _interval):
                return ["ALTUSDT"]

            def active_waterfall_positions(self):
                return []

        with patch("pump_dump_hunter.waterfall.build_broad_universe", side_effect=RuntimeError("HTTP 418")), patch(
            "pump_dump_hunter.waterfall.prewarm_waterfall_symbols"
        ) as prewarm:
            symbols = refresh_waterfall_universe(object(), FakeStore(), engine, self.settings, 450, 1)
        self.assertEqual(symbols, ["ALTUSDT"])
        self.assertFalse(prewarm.call_args.kwargs["allow_rest"])

    @staticmethod
    def _candle(
        symbol: str,
        open_time: int,
        open_: float,
        close: float,
        quote_volume: float,
        *,
        high: float | None = None,
        low: float | None = None,
    ) -> Candle:
        return Candle(
            symbol=symbol,
            interval="1m",
            open_time=open_time,
            open=open_,
            high=max(open_, close) if high is None else high,
            low=min(open_, close) if low is None else low,
            close=close,
            volume=100.0,
            close_time=open_time + 59_999,
            quote_volume=quote_volume,
            trades=100,
            taker_buy_base=40.0,
            taker_buy_quote=quote_volume * 0.4,
        )


if __name__ == "__main__":
    unittest.main()
