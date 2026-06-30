from __future__ import annotations

import unittest

from pump_dump_hunter.engine.signal_engine import SignalEngine
from pump_dump_hunter.live import build_watch_symbols
from pump_dump_hunter.models import PumpEvent
from tests.helpers import pump_dump_1m, temp_settings


class LiveMonitorTests(unittest.TestCase):
    def test_watch_symbols_keeps_active_pump_even_if_not_selected(self):
        settings = temp_settings()
        engine = SignalEngine(settings)
        engine.load_events([
            PumpEvent(
                event_id="OLDUSDT-1",
                symbol="OLDUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=9_999_999_999_999,
                trigger_window="15m",
                anchor_price=1.0,
                high_price=2.0,
                high_time=1,
                current_price=1.5,
                max_gain_pct=100.0,
            ),
            PumpEvent(
                event_id="妖币USDT-1",
                symbol="妖币USDT",
                first_seen=1,
                last_seen=1,
                expires_at=9_999_999_999_999,
                trigger_window="15m",
                anchor_price=1.0,
                high_price=2.0,
                high_time=1,
                current_price=1.5,
                max_gain_pct=100.0,
            ),
        ])
        self.assertEqual(build_watch_symbols(engine, ["NEWUSDT"]), ["NEWUSDT", "OLDUSDT"])

    def test_prime_candles_does_not_mark_alerted(self):
        settings = temp_settings()
        engine = SignalEngine(settings)
        event = PumpEvent(
            event_id="PUMPUSDT-1",
            symbol="PUMPUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=9_999_999_999_999,
            trigger_window="30m",
            anchor_price=100.0,
            high_price=150.0,
            high_time=1,
            current_price=150.0,
            max_gain_pct=50.0,
        )
        engine.load_events([event])
        changed = engine.prime_candles(pump_dump_1m()[100:130])
        self.assertTrue(changed)
        warmed = engine.events_by_symbol["PUMPUSDT"]
        self.assertIsNone(warmed.early_alerted_after_high_time)
        self.assertIsNone(warmed.short_alerted_after_high_time)
        self.assertGreater(len(engine.buffers[("PUMPUSDT", "1m")]), 0)


if __name__ == "__main__":
    unittest.main()