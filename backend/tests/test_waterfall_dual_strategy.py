from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import patch

from pump_dump_hunter.board_waterfall import BoardWaterfallEngine, STRATEGY_NAME
from pump_dump_hunter.data.store import Store
from pump_dump_hunter.models import Candle, KlineClosed
from pump_dump_hunter.timeutils import closed_candle_cutoff_ms, utc_ms
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
        # flow gate off: this test verifies the base E1 trailing-exit mechanics.
        settings = {**self.settings, "claude_board_waterfall": {"exit_flow_gate_enabled": False}}
        engine = BoardWaterfallEngine(settings)
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

    def test_board_24h_return_uses_timestamp_not_row_offset(self) -> None:
        engine = BoardWaterfallEngine(self.settings)
        start = 1_700_000_000_000
        rows = []
        for i in range(-1, 1441):
            if i == 100:
                continue
            close = 80.0 if i == -1 else (140.0 if i == 1440 else 100.0)
            rows.append(self._candle("GAPUSDT", start + i * 60_000, close, close, 10_000.0))
        engine.prime_candles(rows)
        watch = engine.watch_row("GAPUSDT")
        self.assertIsNotNone(watch)
        self.assertAlmostEqual(watch["ret_24h"], 0.40)

    def test_board_entry_fails_closed_until_24h_gap_is_repaired(self) -> None:
        engine = BoardWaterfallEngine(self.settings)
        start = 1_700_000_000_000
        history = [
            self._candle("GAPUSDT", start + i * 60_000, 100.0, 100.0, 10_000.0)
            for i in range(-1, 1440)
            if i != 100
        ]
        engine.prime_candles(history)
        trigger = self._candle(
            "GAPUSDT", start + 1440 * 60_000, 160.0, 148.0, 20_000.0,
            high=160.0, low=147.0,
        )
        _, _, signals = engine.on_kline(KlineClosed("GAPUSDT", "1m", trigger))
        self.assertEqual(signals, [])
        engine.prime_candles([
            self._candle("GAPUSDT", start + 100 * 60_000, 100.0, 100.0, 10_000.0),
        ])
        self.assertIn(start + 100 * 60_000, {c.open_time for c in engine.candles["GAPUSDT"]})

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
            save_calls = 0

            def load_candles(self, *_args, **_kwargs):
                return [cached]

            def save_candles(self, _rows):
                self.save_calls += 1

            def upsert_waterfall_watch(self, _rows):
                return None

        class FakeClient:
            calls = 0

            def klines(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("REST must not be called in DB-only mode")

        client = FakeClient()
        store = FakeStore()
        prewarm_waterfall_symbols(client, store, engine, ["ALTUSDT"], 1500, 1, allow_rest=False)
        self.assertEqual(client.calls, 0)
        self.assertEqual(store.save_calls, 0)

    def test_current_thin_new_listing_cache_does_not_refetch_full_history(self) -> None:
        engine = WaterfallEngine(self.settings)
        now = closed_candle_cutoff_ms(utc_ms(), "1m") - 59_999
        cached = self._candle("NEWUSDT", now, 1.0, 1.01, 10_000.0)

        class FakeStore:
            save_calls = 0

            def load_candles(self, *_args, **_kwargs):
                return [cached]

            def save_candles(self, _rows):
                self.save_calls += 1

            def upsert_waterfall_watch(self, _rows):
                return None

        class FakeClient:
            calls = 0

            def klines(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("a current newly-listed cache must not be refetched")

        store = FakeStore()
        client = FakeClient()
        prewarm_waterfall_symbols(client, store, engine, ["NEWUSDT"], 1500, 1)
        self.assertEqual(client.calls, 0)
        self.assertEqual(store.save_calls, 0)

    def test_db_only_prewarm_does_not_rewrite_cached_candles(self) -> None:
        engine = WaterfallEngine(self.settings)
        cached = self._candle("ALTUSDT", 1_700_000_000_000, 1.0, 1.0, 1000.0)

        class FakeStore:
            saved: list[list[Candle]] = []

            def load_candles(self, *_args, **_kwargs):
                return [cached]

            def save_candles(self, rows):
                self.saved.append(list(rows))

            def upsert_waterfall_watch(self, _rows):
                return None

        store = FakeStore()
        prewarm_waterfall_symbols(object(), store, engine, ["ALTUSDT"], 1500, 1, allow_rest=False)
        self.assertEqual(store.saved, [])

    def test_stale_near_full_cache_fetches_only_missing_tail(self) -> None:
        engine = WaterfallEngine(self.settings)
        cutoff = closed_candle_cutoff_ms(utc_ms(), "1m")
        target_open = cutoff - 59_999
        cached = [
            self._candle("TAILUSDT", target_open - (1499 - i) * 60_000, 1.0, 1.0, 10_000.0)
            for i in range(1490)
        ]

        class FakeStore:
            def load_candles(self, *_args, **_kwargs):
                return cached

            def save_candles(self, _rows):
                return None

            def upsert_waterfall_watch(self, _rows):
                return None

        class FakeClient:
            limits: list[int] = []

            def klines(self, symbol, interval, limit):
                self.limits.append(limit)
                return [
                    WaterfallRuntimeRegressionTests._candle(
                        symbol, target_open - (limit - 1 - i) * 60_000, 1.0, 1.0, 10_000.0,
                    )
                    for i in range(limit)
                ]

        client = FakeClient()
        prewarm_waterfall_symbols(client, FakeStore(), engine, ["TAILUSDT"], 1500, 1)
        self.assertEqual(client.limits, [11])

    def test_periodic_refresh_skips_resident_symbols_and_prunes_removed(self) -> None:
        engine = WaterfallEngine(self.settings)
        cutoff = closed_candle_cutoff_ms(utc_ms(), "1m")
        engine._append(self._candle("KEEPUSDT", cutoff - 59_999, 1.0, 1.0, 1000.0))
        engine._append(self._candle("REMOVEUSDT", cutoff - 59_999, 1.0, 1.0, 1000.0))

        class FakeStore:
            def active_waterfall_positions(self, strategy=""):
                return []

        with patch(
            "pump_dump_hunter.waterfall.build_broad_universe",
            return_value=[{"symbol": "KEEPUSDT"}],
        ), patch("pump_dump_hunter.waterfall.prewarm_waterfall_symbols") as prewarm:
            symbols = refresh_waterfall_universe(object(), FakeStore(), engine, self.settings, 450, 1)

        self.assertEqual(symbols, ["KEEPUSDT"])
        self.assertFalse(prewarm.called)
        self.assertIn("KEEPUSDT", engine.candles)
        self.assertNotIn("REMOVEUSDT", engine.candles)

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


class BoardFlowGateExitTests(unittest.TestCase):
    START = 1_700_000_000_000

    @staticmethod
    def _flow_candle(open_time: int, tsell: float) -> Candle:
        qv = 10_000.0
        return Candle(
            symbol="SYM", interval="1m", open_time=open_time, open=100.0, high=100.0,
            low=100.0, close=100.0, volume=100.0, close_time=open_time + 59_999,
            quote_volume=qv, trades=100, taker_buy_base=1.0,
            taker_buy_quote=qv * (1.0 - tsell),  # tsell = 1 - tbq/qv
        )

    def _engine(self, tsell: float, **cfg) -> BoardWaterfallEngine:
        settings = {**temp_settings(), "claude_board_waterfall": cfg}
        eng = BoardWaterfallEngine(settings)
        dq: deque = deque(maxlen=eng.maxlen)
        for i in range(20):
            dq.append(self._flow_candle(self.START + i * 60_000, tsell))
        eng.candles["SYM"] = dq
        return eng

    def test_flow_hold_through_threshold_and_guards(self) -> None:
        self.assertTrue(self._engine(0.60)._flow_hold_through("SYM"))    # sellers dominant -> hold
        self.assertFalse(self._engine(0.30)._flow_hold_through("SYM"))   # buyers back -> exit
        self.assertFalse(self._engine(0.60, exit_flow_gate_enabled=False)._flow_hold_through("SYM"))
        eng = self._engine(0.60)
        eng.candles["SYM"] = deque(list(eng.candles["SYM"])[:3], maxlen=eng.maxlen)  # < W+1 bars
        self.assertFalse(eng._flow_hold_through("SYM"))

    def _position(self) -> WaterfallPosition:
        return WaterfallPosition(
            position_id="cbwf-SYM-1", symbol="SYM", strategy=STRATEGY_NAME,
            family="board_waterfall", rule="board40_drop7_60m", exit_profile="claude_e1",
            status="open", side="SHORT", entry_time=self.START, entry_price=100.0,
            notional_usdt=200.0, stop_price=103.0, best_price=94.0, worst_price=100.0,
            trail_price=96.0, fee_rate=0.0008, margin_usdt=20.0, leverage=10.0,
            capital_fraction=0.2, updated_time=self.START,
        )

    def test_rebound_held_when_sellers_dominant(self) -> None:
        eng = self._engine(0.60)  # tsell 0.60 >= 0.48 -> hold through the take-profit
        eng.positions["SYM"] = self._position()
        rebound = Candle("SYM", "1m", self.START + 20 * 60_000, 95.0, 97.0, 94.5, 96.0,
                         100.0, self.START + 20 * 60_000 + 59_999, 10_000.0, 100, 1.0, 4000.0)
        _, _, signals = eng.on_kline(KlineClosed("SYM", "1m", rebound))
        self.assertEqual(signals, [])                       # take-profit skipped
        self.assertIn("SYM", eng.positions)                 # still holding

    def test_rebound_takes_profit_when_buyers_return(self) -> None:
        eng = self._engine(0.30)  # tsell 0.30 < 0.48 -> real bounce -> take profit
        eng.positions["SYM"] = self._position()
        rebound = Candle("SYM", "1m", self.START + 20 * 60_000, 95.0, 97.0, 94.5, 96.0,
                         100.0, self.START + 20 * 60_000 + 59_999, 10_000.0, 100, 1.0, 7000.0)
        _, changed, signals = eng.on_kline(KlineClosed("SYM", "1m", rebound))
        self.assertEqual([s.action for s in signals], ["take_profit"])
        self.assertNotIn("SYM", eng.positions)

    def test_stop_loss_never_gated_by_flow(self) -> None:
        eng = self._engine(0.60)  # sellers dominant, but a stop must still fire
        eng.positions["SYM"] = self._position()
        spike = Candle("SYM", "1m", self.START + 20 * 60_000, 102.0, 104.0, 101.0, 103.5,
                       100.0, self.START + 20 * 60_000 + 59_999, 10_000.0, 100, 1.0, 4000.0)
        _, _, signals = eng.on_kline(KlineClosed("SYM", "1m", spike))
        self.assertEqual([s.action for s in signals], ["stop_loss"])


if __name__ == "__main__":
    unittest.main()
