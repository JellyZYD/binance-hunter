from __future__ import annotations

import unittest

from pump_dump_hunter.discovery import compute_liquidity_records
from pump_dump_hunter.engine.signal_engine import SignalEngine
from pump_dump_hunter.engine.signal_engine import breaks_post_high_structure
from pump_dump_hunter.models import Candle, KlineClosed, PumpEvent, SignalParams
from tests.helpers import pump_dump_1m, temp_settings


class SignalEngineTests(unittest.TestCase):
    def test_pump_then_dump_emits_short_once(self):
        settings = temp_settings()
        settings["signals"]["volume_window"] = 3
        rows = pump_dump_1m()
        params = SignalParams(confirm_min_remaining_pct=15.0)
        engine = SignalEngine(settings, params=params)
        cutoff = rows[104].close_time
        records = compute_liquidity_records(
            [{"symbol": "PUMPUSDT", "pct_24h": 0.0}],
            {"PUMPUSDT": rows[:105]},
            top_n=1,
            data_cutoff_time=cutoff,
            params=params,
        )
        changed = engine.on_discovery(records, cutoff)
        self.assertTrue(changed)
        alerts = []
        # Feed 5m candles through backtest aggregation path in a focused way.
        from pump_dump_hunter.backtest import aggregate_1m_to_5m

        for candle in aggregate_1m_to_5m(rows[:140]):
            _changed, new = engine.on_kline(KlineClosed(candle.symbol, "5m", candle))
            alerts.extend(new)
        from pump_dump_hunter.backtest import aggregate_1m_to_interval

        for candle in aggregate_1m_to_interval(rows[:140], "15m"):
            _changed, new = engine.on_kline(KlineClosed(candle.symbol, "15m", candle))
            alerts.extend(new)
        levels = [a.level for a in alerts]
        self.assertIn("short_signal", levels)
        self.assertEqual(levels.count("short_signal"), 1)

    def test_top_rejection_emits_early_alert(self):
        settings = temp_settings()
        settings["signals"]["volume_window"] = 3
        engine = SignalEngine(settings)
        engine.load_events(
            [
                PumpEvent(
                    event_id="PUMPUSDT-1",
                    symbol="PUMPUSDT",
                    first_seen=1,
                    last_seen=1,
                    expires_at=10_000_000,
                    trigger_window="1d",
                    anchor_price=100.0,
                    high_price=153.0,
                    high_time=1_000,
                    current_price=150.0,
                    max_gain_pct=53.0,
                )
            ]
        )
        step = 900_000
        rows = [
            Candle("PUMPUSDT", "15m", 1_000 + i * step, 149.0, 151.0, 148.0, 150.0 + (i % 2), 100.0, 1_000 + (i + 1) * step - 1, 1000.0, 100)
            for i in range(3)
        ]
        rows.append(Candle("PUMPUSDT", "15m", 1_000 + 3 * step, 151.0, 158.0, 150.0, 151.5, 100.0, 1_000 + 4 * step - 1, 5000.0, 100))
        alerts = []
        for candle in rows:
            _changed, new = engine.on_kline(KlineClosed(candle.symbol, "15m", candle))
            alerts.extend(new)
        self.assertEqual([a.level for a in alerts], ["early_alert"])

    def test_two_bar_rejection_emits_early_alert(self):
        settings = temp_settings()
        settings["signals"]["volume_window"] = 3
        engine = SignalEngine(settings)
        engine.load_events(
            [
                PumpEvent(
                    event_id="PUMPUSDT-2",
                    symbol="PUMPUSDT",
                    first_seen=1,
                    last_seen=1,
                    expires_at=10_000_000,
                    trigger_window="1d",
                    anchor_price=100.0,
                    high_price=162.0,
                    high_time=1_000,
                    current_price=150.0,
                    max_gain_pct=62.0,
                )
            ]
        )
        step = 900_000
        rows = [
            Candle("PUMPUSDT", "15m", 1_000 + i * step, 149.0, 151.0, 148.0, 150.0 + (i % 2), 100.0, 1_000 + (i + 1) * step - 1, 1000.0, 100)
            for i in range(3)
        ]
        rows.append(Candle("PUMPUSDT", "15m", 1_000 + 3 * step, 151.0, 162.0, 150.0, 159.0, 100.0, 1_000 + 4 * step - 1, 4000.0, 100))
        rows.append(Candle("PUMPUSDT", "15m", 1_000 + 4 * step, 159.0, 160.0, 149.0, 151.0, 100.0, 1_000 + 5 * step - 1, 6000.0, 100))
        alerts = []
        for candle in rows:
            _changed, new = engine.on_kline(KlineClosed(candle.symbol, "15m", candle))
            alerts.extend(new)
        self.assertEqual([a.level for a in alerts], ["early_alert"])

    def test_prime_candles_ignores_duplicate_open_time(self):
        settings = temp_settings()
        engine = SignalEngine(settings)
        row = pump_dump_1m()[0]
        engine.prime_candles([row, row])
        self.assertEqual(len(engine.buffers[(row.symbol, row.interval)]), 1)

    def test_structure_break_uses_body_low_not_lower_wick(self):
        rows = [
            Candle("PUMPUSDT", "15m", 1_000, 150.0, 151.0, 90.0, 148.0, 100.0, 1_999, 1000.0, 100),
            Candle("PUMPUSDT", "15m", 2_000, 149.0, 150.0, 89.0, 147.0, 100.0, 2_999, 1000.0, 100),
            Candle("PUMPUSDT", "15m", 3_000, 147.0, 148.0, 130.0, 146.0, 100.0, 3_999, 1000.0, 100),
        ]
        self.assertGreater(rows[-1].close, min(c.low for c in rows[:-1]))
        self.assertTrue(breaks_post_high_structure(rows, high_time=999, lookback=3))

        no_break = rows[:-1] + [
            Candle("PUMPUSDT", "15m", 3_000, 147.0, 148.0, 130.0, 147.2, 100.0, 3_999, 1000.0, 100)
        ]
        self.assertFalse(breaks_post_high_structure(no_break, high_time=999, lookback=3))


if __name__ == "__main__":
    unittest.main()
