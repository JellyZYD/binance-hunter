from __future__ import annotations

import unittest

from pump_dump_hunter.backtest import run_backtest_from_store, slice_candles, split_train_validation, summarize_rows
from pump_dump_hunter.data.store import Store
from pump_dump_hunter.models import SignalParams
from tests.helpers import candle, pump_dump_1m, temp_settings


class BacktestTests(unittest.TestCase):
    def test_backtest_replay_generates_short_signal(self):
        settings = temp_settings()
        settings["signals"]["volume_window"] = 3
        store = Store(settings["paths"]["db_path"])
        store.save_candles(pump_dump_1m())
        result = run_backtest_from_store(store, settings, SignalParams(confirm_min_remaining_pct=15.0), days=1, top_n=1)
        self.assertGreaterEqual(result.metrics["short_signals"], 1)

    def test_train_validation_split_uses_decision_time(self):
        rows = [
            {"level": "short_signal", "decision_time": 10, "ret_30m": 0.1},
            {"level": "short_signal", "decision_time": 90, "ret_30m": -0.1},
        ]
        split = split_train_validation(rows, start=0, end=100, train_ratio=0.7)
        self.assertEqual(split["train"]["short_signals"], 1)
        self.assertEqual(split["validation"]["short_signals"], 1)
        self.assertGreater(split["train"]["avg_ret_30m"], 0)
        self.assertLess(split["validation"]["avg_ret_30m"], 0)

    def test_slice_candles_uses_exclusive_start_and_inclusive_end(self):
        rows = [candle("PUMPUSDT", "1m", 1_700_000_000_000 + i * 60_000, 100.0, 100.0, 1.0) for i in range(5)]
        close_times = [c.close_time for c in rows]
        sliced = slice_candles(rows, close_times, close_times[1], close_times[3])
        self.assertEqual([c.open_time for c in sliced], [rows[2].open_time, rows[3].open_time])

    def test_summarize_rows_includes_long_fish_body_metrics(self):
        summary = summarize_rows(
            [
                {
                    "level": "short_signal",
                    "max_favorable": 0.12,
                    "max_adverse": 0.03,
                    "max_adverse_before_5pct": 0.01,
                    "time_to_best_m": 240,
                    "time_to_3pct_m": 30,
                    "time_to_5pct_m": 90,
                    "time_to_10pct_m": 180,
                    "capture_to_anchor_ratio": 0.8,
                    "reached_anchor": False,
                },
                {
                    "level": "short_signal",
                    "max_favorable": 0.04,
                    "max_adverse": 0.02,
                    "max_adverse_before_5pct": 0.02,
                    "time_to_best_m": 120,
                    "time_to_3pct_m": 60,
                    "capture_to_anchor_ratio": 1.0,
                    "reached_anchor": True,
                },
            ]
        )
        self.assertEqual(summary["hit_rate_3pct"], 1.0)
        self.assertEqual(summary["hit_rate_5pct"], 0.5)
        self.assertEqual(summary["hit_rate_10pct"], 0.5)
        self.assertAlmostEqual(summary["fish_body_ratio_10pct"], 0.5)
        self.assertAlmostEqual(summary["avg_capture_to_anchor_ratio"], 0.9)
        self.assertAlmostEqual(summary["reached_anchor_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
