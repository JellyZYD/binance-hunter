from __future__ import annotations

import unittest

from pump_dump_hunter.discovery import compute_liquidity_records
from pump_dump_hunter.engine.signal_engine import SignalEngine
from pump_dump_hunter.engine.signal_engine import breaks_post_high_structure
from pump_dump_hunter.models import Alert, Candle, KlineClosed, LongEvent, PumpEvent, SignalParams
from tests.helpers import pump_dump_1m, temp_settings


class SignalEngineTests(unittest.TestCase):
    def test_ml_exit_signal_skips_long_signal_on_same_candle(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["long_enabled"] = True
        engine = SignalEngine(settings)
        engine.load_events([
            PumpEvent(
                event_id="PUMPUSDT-1",
                symbol="PUMPUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=10_000_000,
                trigger_window="1d",
                anchor_price=100.0,
                high_price=150.0,
                high_time=1_000,
                current_price=145.0,
                max_gain_pct=50.0,
            )
        ])
        engine.load_long_events([
            LongEvent(
                event_id="PUMPUSDT-L-1",
                symbol="PUMPUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=10_000_000,
                entry_price=120.0,
                high_price=150.0,
                current_price=145.0,
            )
        ])

        candle = Candle("PUMPUSDT", "15m", 2_000, 145.0, 146.0, 130.0, 138.0, 100.0, 901_999, 5000.0, 100)
        engine._ml_signals = lambda pump, c: [dummy_alert("short_signal", pump.event_id, pump.symbol, c)]  # type: ignore[method-assign]
        engine._process_long = lambda c: [dummy_alert("long_signal", "PUMPUSDT-L-1", c.symbol, c)]  # type: ignore[method-assign]

        _changed, alerts = engine.on_kline(KlineClosed(candle.symbol, "15m", candle))

        self.assertEqual([a.level for a in alerts], ["short_signal"])
        self.assertEqual(engine.long_events_by_symbol["PUMPUSDT"].status, "closed")
        self.assertEqual(engine.long_events_by_symbol["PUMPUSDT"].exit_reason, "下跌启动")

    def test_long_event_runs_exit_before_long_signal_without_pump_event(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["long_enabled"] = True
        engine = SignalEngine(settings)
        engine.load_long_events([
            LongEvent(
                event_id="PUMPUSDT-L-1",
                symbol="PUMPUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=10_000_000,
                entry_price=120.0,
                high_price=150.0,
                current_price=145.0,
            )
        ])

        candle = Candle("PUMPUSDT", "15m", 2_000, 145.0, 146.0, 130.0, 138.0, 100.0, 901_999, 5000.0, 100)
        engine._long_exit_signals = lambda le, c: [dummy_alert("early_alert", le.event_id, le.symbol, c)]  # type: ignore[method-assign]
        engine._long_signals = lambda le, c: [dummy_alert("long_signal", le.event_id, le.symbol, c)]  # type: ignore[method-assign]

        _changed, alerts = engine.on_kline(KlineClosed(candle.symbol, "15m", candle))

        self.assertEqual([a.level for a in alerts], ["early_alert"])

    def test_long_timeout_and_invalid_emit_status_alerts(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["long_enabled"] = True
        engine = SignalEngine(settings)
        engine.load_long_events([
            LongEvent(
                event_id="TIMEUSDT-L-1",
                symbol="TIMEUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=1_999,
                entry_price=100.0,
                high_price=105.0,
                current_price=100.0,
            ),
            LongEvent(
                event_id="BADUSDT-L-1",
                symbol="BADUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=10_000_000,
                entry_price=100.0,
                high_price=105.0,
                current_price=100.0,
            ),
        ])

        timeout_candle = Candle("TIMEUSDT", "15m", 2_000, 100.0, 101.0, 99.0, 100.0, 100.0, 901_999, 1000.0, 100)
        invalid_candle = Candle("BADUSDT", "15m", 2_000, 100.0, 101.0, 91.0, 91.0, 100.0, 901_999, 1000.0, 100)

        _changed, timeout_alerts = engine.on_kline(KlineClosed(timeout_candle.symbol, "15m", timeout_candle))
        _changed, invalid_alerts = engine.on_kline(KlineClosed(invalid_candle.symbol, "15m", invalid_candle))

        self.assertEqual([a.level for a in timeout_alerts], ["long_timeout"])
        self.assertEqual([a.level for a in invalid_alerts], ["long_invalid"])

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


def dummy_alert(level: str, event_id: str, symbol: str, candle: Candle) -> Alert:
    return Alert(
        alert_id=f"{event_id}-{level}-{candle.close_time}",
        event_id=event_id,
        symbol=symbol,
        level=level,
        decision_time=candle.close_time,
        source_candle_close_time=candle.close_time,
        data_cutoff_time=candle.close_time,
        price=candle.close,
        invalidation_price=candle.high,
        anchor_price=candle.open,
        high_price=candle.high,
        remaining_downside_pct=0.0,
        volume_ratio=0.0,
        evidence=[],
        risks=[],
    )


if __name__ == "__main__":
    unittest.main()
