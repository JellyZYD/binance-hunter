import unittest

from pump_dump_hunter.data.store import Store
from pump_dump_hunter.web import annotate_pumps

from .helpers import candle, temp_settings


class WebApiTests(unittest.TestCase):
    def test_candle_stats_are_shared_between_dashboard_polls(self):
        settings = temp_settings()
        store = Store(settings["paths"]["db_path"])
        store.save_candles([candle("ONEUSDT", "1m", 1_700_000_000_000, 1.0, 1.0)])
        self.assertEqual(store.candle_stat_1m(cache_seconds=300)["count"], 1)

        store.save_candles([candle("ONEUSDT", "1m", 1_700_000_060_000, 1.0, 1.0)])
        self.assertEqual(store.candle_stat_1m(cache_seconds=300)["count"], 1)
        self.assertEqual(store.candle_stat_1m(cache_seconds=0)["count"], 2)

    def test_annotate_pumps_uses_formal_and_long_derived_thresholds(self):
        rows = [
            {"symbol": "LOWUSDT", "max_gain_pct": 20.0, "evidence": []},
            {"symbol": "PUMPUSDT", "max_gain_pct": 26.0, "evidence": []},
            {"symbol": "LONGUSDT", "max_gain_pct": 16.0, "evidence": ["source=long_signal_pump_watch"]},
        ]

        out = annotate_pumps(rows, formal_min_gain_pct=25.0, long_min_gain_pct=15.0)
        by_symbol = {row["symbol"]: row for row in out}

        self.assertFalse(by_symbol["LOWUSDT"]["is_formal_watch"])
        self.assertEqual(by_symbol["LOWUSDT"]["monitor_stage"], "shadow")
        self.assertTrue(by_symbol["PUMPUSDT"]["is_formal_watch"])
        self.assertEqual(by_symbol["PUMPUSDT"]["formal_watch_min_gain_pct"], 25.0)
        self.assertTrue(by_symbol["LONGUSDT"]["is_formal_watch"])
        self.assertTrue(by_symbol["LONGUSDT"]["long_derived_watch"])
        self.assertEqual(by_symbol["LONGUSDT"]["formal_watch_min_gain_pct"], 15.0)


if __name__ == "__main__":
    unittest.main()
