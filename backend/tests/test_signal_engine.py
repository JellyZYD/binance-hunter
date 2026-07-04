from __future__ import annotations

import unittest
from unittest.mock import patch

from pump_dump_hunter.discovery import compute_liquidity_records
from pump_dump_hunter.data.store import Store
from pump_dump_hunter.engine.signal_engine import SignalEngine
from pump_dump_hunter.engine.signal_engine import breaks_post_high_structure
from pump_dump_hunter.models import Alert, Candle, KlineClosed, LiquidityRecord, LongEvent, PumpEvent, SignalParams
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

    def test_pump_signal_rearms_only_after_new_high_and_cooldown(self):
        settings = temp_settings()
        settings["signals"]["multi_signal_cooldown_hours"] = 4.0
        engine = SignalEngine(settings)
        pump = PumpEvent(
            event_id="PUMPUSDT-1",
            symbol="PUMPUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=100_000_000,
            trigger_window="1d",
            anchor_price=100.0,
            high_price=150.0,
            high_time=1_000,
            current_price=140.0,
            max_gain_pct=50.0,
        )

        engine._mark_pump_signal(pump, "short_signal", 10_000)

        self.assertFalse(engine._can_emit_pump_signal(pump, "short_signal", 10_000 + 4 * 3_600_000 - 1))
        self.assertFalse(engine._can_emit_pump_signal(pump, "short_signal", 10_000 + 4 * 3_600_000))
        pump.high_time = 20_000
        self.assertTrue(engine._can_emit_pump_signal(pump, "short_signal", 10_000 + 4 * 3_600_000))
        self.assertTrue(engine._can_emit_pump_signal(pump, "early_alert", 10_000))

    def test_long_signal_uses_independent_cooldown(self):
        settings = temp_settings()
        settings["signals"]["long_signal_cooldown_hours"] = 2.0
        engine = SignalEngine(settings)
        le = LongEvent(
            event_id="LONGUSDT-L-1",
            symbol="LONGUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=100_000_000,
            entry_price=100.0,
            high_price=105.0,
            current_price=104.0,
            long_last_signal_time=10_000,
        )

        self.assertFalse(engine._can_emit_long_signal(le, 10_000 + 2 * 3_600_000 - 1))
        self.assertTrue(engine._can_emit_long_signal(le, 10_000 + 2 * 3_600_000))

    def test_discovery_does_not_create_long_after_pump_short_signal(self):
        settings = temp_settings()
        settings["signals"]["long_enabled"] = True
        engine = SignalEngine(settings)
        engine.load_events([
            PumpEvent(
                event_id="NOMUSDT-1",
                symbol="NOMUSDT",
                first_seen=1,
                last_seen=10_000,
                expires_at=1_000_000,
                trigger_window="1d",
                anchor_price=0.0015,
                high_price=0.0023,
                high_time=10_000,
                current_price=0.0020,
                max_gain_pct=50.0,
                short_last_alert_time=10_000,
            )
        ])
        record = LiquidityRecord(
            symbol="NOMUSDT",
            rank=1,
            last_price=0.0021,
            quote_volume_15m=100_000.0,
            quote_volume_30m=220_000.0,
            pct_15m=2.0,
            pct_30m=5.0,
            amp_15m=3.0,
            amp_30m=6.0,
            volume_ratio_15m=1.5,
            volume_ratio_30m=2.2,
            gain_rank_15m=5,
            gain_rank_30m=3,
            selected=True,
            pump_qualified=False,
            data_cutoff_time=20_000,
            qvol30_rank=2,
            ret30_rank=3,
            qvol30_rank_pct=0.02,
            ret30_rank_pct=0.03,
            long_candidate=True,
        )

        changed = engine.on_discovery([record], 20_000)

        self.assertEqual(changed, [])
        self.assertNotIn("NOMUSDT", engine.long_events_by_symbol)

    def test_discovery_closes_long_event_when_candidate_lost(self):
        settings = temp_settings()
        settings["signals"]["long_enabled"] = True
        engine = SignalEngine(settings)
        engine.load_long_events([
            LongEvent(
                event_id="NOMUSDT-L-1",
                symbol="NOMUSDT",
                first_seen=1,
                last_seen=1,
                expires_at=1_000_000,
                entry_price=0.0020,
                high_price=0.0022,
                current_price=0.0021,
            )
        ])
        record = LiquidityRecord(
            symbol="NOMUSDT",
            rank=1,
            last_price=0.0020,
            quote_volume_15m=100_000.0,
            quote_volume_30m=220_000.0,
            pct_15m=-1.0,
            pct_30m=1.0,
            amp_15m=3.0,
            amp_30m=6.0,
            volume_ratio_15m=1.0,
            volume_ratio_30m=1.1,
            gain_rank_15m=80,
            gain_rank_30m=90,
            selected=True,
            pump_qualified=False,
            data_cutoff_time=20_000,
            qvol30_rank=2,
            ret30_rank=90,
            qvol30_rank_pct=0.02,
            ret30_rank_pct=0.90,
            long_candidate=False,
        )

        engine.on_discovery([record], 20_000)

        self.assertEqual(engine.long_events_by_symbol["NOMUSDT"].status, "closed")
        self.assertEqual(engine.long_events_by_symbol["NOMUSDT"].exit_reason, "long_candidate_lost")

    def test_existing_long_event_closes_when_pump_risk_signal_is_active(self):
        settings = temp_settings()
        settings["signals"]["long_enabled"] = True
        engine = SignalEngine(settings)
        engine.load_events([
            PumpEvent(
                event_id="NOMUSDT-1",
                symbol="NOMUSDT",
                first_seen=1,
                last_seen=10_000,
                expires_at=1_000_000,
                trigger_window="1d",
                anchor_price=0.0015,
                high_price=0.0023,
                high_time=10_000,
                current_price=0.0020,
                max_gain_pct=50.0,
                early_last_alert_time=10_000,
            )
        ])
        engine.load_long_events([
            LongEvent(
                event_id="NOMUSDT-L-1",
                symbol="NOMUSDT",
                first_seen=5_000,
                last_seen=5_000,
                expires_at=1_000_000,
                entry_price=0.0020,
                high_price=0.0021,
                current_price=0.00205,
            )
        ])
        engine._long_signals = lambda le, c: [dummy_alert("long_signal", le.event_id, le.symbol, c)]  # type: ignore[method-assign]
        candle = Candle("NOMUSDT", "5m", 20_000, 0.00205, 0.0021, 0.0020, 0.00208, 100.0, 319_999, 5000.0, 100)

        alerts = engine._process_long(candle)

        self.assertEqual(alerts, [])
        self.assertEqual(engine.long_events_by_symbol["NOMUSDT"].status, "closed")
        self.assertEqual(engine.long_events_by_symbol["NOMUSDT"].exit_reason, "pump_risk_conflict")

    def test_lifecycle_fast_route_warns_and_pullback_shorts(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["strategy_version"] = "lifecycle_expert"
        settings["signals"]["lifecycle_route_confirm_bars"] = 1
        engine = SignalEngine(settings)
        engine.ml = DummyLifecycleScorer()
        pump = PumpEvent(
            event_id="NOMUSDT-1",
            symbol="NOMUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=10_000_000,
            trigger_window="1d",
            anchor_price=0.0015,
            high_price=0.0023,
            high_time=1_000,
            current_price=0.0020,
            max_gain_pct=50.0,
        )
        candle = Candle("NOMUSDT", "15m", 2_000, 0.0020, 0.0022, 0.0019, 0.00205, 100.0, 901_999, 5000.0, 100)
        engine._append_candle(candle)

        with patch("pump_dump_hunter.engine.signal_engine.life.build_lifecycle_row", return_value={"behavior_state": "distribution", "f": 1.0}):
            alerts = engine._lifecycle_signals(pump, candle)

        self.assertEqual([a.level for a in alerts], ["early_alert"])
        self.assertEqual(alerts[0].model_name, "fast_top")

        pump2 = PumpEvent(
            event_id="NOMUSDT-2",
            symbol="NOMUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=10_000_000,
            trigger_window="1d",
            anchor_price=0.0015,
            high_price=0.0023,
            high_time=1_000,
            current_price=0.0020,
            max_gain_pct=50.0,
        )
        with patch("pump_dump_hunter.engine.signal_engine.life.build_lifecycle_row", return_value={"behavior_state": "pullback_risk", "f": 1.0}):
            alerts = engine._lifecycle_signals(pump2, candle)

        self.assertEqual([a.level for a in alerts], ["short_signal"])
        self.assertEqual(alerts[0].model_name, "fast_short")

    def test_lifecycle_unknown_route_blocks_experts(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["strategy_version"] = "lifecycle_router_expert"
        settings["signals"]["lifecycle_route_confirm_bars"] = 1
        engine = SignalEngine(settings)
        engine.ml = DummyLifecycleScorer(probs={"fast_dump": 0.4, "slow_distribution": 0.3, "second_distribution": 0.1, "continuation": 0.2})
        pump = PumpEvent(
            event_id="NOMUSDT-1",
            symbol="NOMUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=10_000_000,
            trigger_window="1d",
            anchor_price=0.0015,
            high_price=0.0023,
            high_time=1_000,
            current_price=0.0020,
            max_gain_pct=50.0,
        )
        candle = Candle("NOMUSDT", "15m", 2_000, 0.0020, 0.0022, 0.0019, 0.00205, 100.0, 901_999, 5000.0, 100)
        engine._append_candle(candle)

        with patch("pump_dump_hunter.engine.signal_engine.life.build_lifecycle_row", return_value={"behavior_state": "breakdown", "f": 1.0}):
            alerts = engine._lifecycle_signals(pump, candle)

        self.assertEqual(alerts, [])
        self.assertEqual(pump.route_mode, "unknown")
        self.assertEqual(pump.lifecycle_mode, "unknown_watch")

    def test_lifecycle_slow_route_only_shorts_on_breakdown(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["strategy_version"] = "lifecycle_router_expert"
        settings["signals"]["lifecycle_route_confirm_bars"] = 1
        engine = SignalEngine(settings)
        engine.ml = DummyLifecycleScorer(probs={"fast_dump": 0.05, "slow_distribution": 0.82, "second_distribution": 0.04, "continuation": 0.02})
        pump = PumpEvent(
            event_id="NOMUSDT-1",
            symbol="NOMUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=10_000_000,
            trigger_window="1d",
            anchor_price=0.0015,
            high_price=0.0023,
            high_time=1_000,
            current_price=0.0020,
            max_gain_pct=50.0,
        )
        candle = Candle("NOMUSDT", "15m", 2_000, 0.0020, 0.0022, 0.0019, 0.00205, 100.0, 901_999, 5000.0, 100)
        engine._append_candle(candle)

        with patch("pump_dump_hunter.engine.signal_engine.life.build_lifecycle_row", return_value={"behavior_state": "distribution", "f": 1.0}):
            alerts = engine._lifecycle_signals(pump, candle)

        self.assertEqual(alerts, [])
        self.assertEqual(pump.lifecycle_mode, "distribution_warning")

        pump2 = PumpEvent(
            event_id="NOMUSDT-2",
            symbol="NOMUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=10_000_000,
            trigger_window="1d",
            anchor_price=0.0015,
            high_price=0.0023,
            high_time=1_000,
            current_price=0.0020,
            max_gain_pct=50.0,
        )
        with patch("pump_dump_hunter.engine.signal_engine.life.build_lifecycle_row", return_value={"behavior_state": "breakdown", "f": 1.0}):
            alerts = engine._lifecycle_signals(pump2, candle)

        self.assertEqual([a.level for a in alerts], ["short_signal"])
        self.assertEqual(alerts[0].model_name, "slow_short")
        self.assertEqual(alerts[0].route_mode, "slow_distribution")

    def test_long_derived_pump_waits_until_mature_before_short_models(self):
        settings = temp_settings()
        settings["signals"]["lifecycle_long_watch_min_gain_pct"] = 15.0
        engine = SignalEngine(settings)
        pump = PumpEvent(
            event_id="LONGUSDT-PW-1",
            symbol="LONGUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=100_000_000,
            trigger_window="long_5m",
            anchor_price=100.0,
            high_price=112.0,
            high_time=1_000,
            current_price=110.0,
            max_gain_pct=12.0,
            evidence=["source=long_signal_pump_watch"],
        )

        ready, reason = engine._lifecycle_pump_signal_ready(pump, {"ctx_high_since_entry": 0.12})

        self.assertFalse(ready)
        self.assertIn("long_watch_not_mature", reason)

        pump.high_price = 116.0
        pump.max_gain_pct = 16.0
        ready, _reason = engine._lifecycle_pump_signal_ready(pump, {"ctx_high_since_entry": 0.16})
        self.assertTrue(ready)

    def test_non_long_pump_waits_until_signal_min_gain(self):
        settings = temp_settings()
        settings["signals"]["lifecycle_pump_signal_min_gain_pct"] = 40.0
        engine = SignalEngine(settings)
        pump = PumpEvent(
            event_id="PUMPUSDT-1",
            symbol="PUMPUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=100_000_000,
            trigger_window="1d",
            anchor_price=100.0,
            high_price=125.0,
            high_time=1_000,
            current_price=120.0,
            max_gain_pct=25.0,
        )

        ready, reason = engine._lifecycle_pump_signal_ready(pump, {"ctx_high_since_entry": 0.25})

        self.assertFalse(ready)
        self.assertIn("pump_watch_not_mature", reason)

        pump.high_price = 145.0
        pump.max_gain_pct = 45.0
        ready, _reason = engine._lifecycle_pump_signal_ready(pump, {"ctx_high_since_entry": 0.45})
        self.assertTrue(ready)

    def test_high_pump_top_bypasses_unknown_route_once(self):
        settings = temp_settings()
        settings["signals"]["mode"] = "ml"
        settings["signals"]["strategy_version"] = "lifecycle_router_expert"
        settings["signals"]["lifecycle_high_pump_enabled"] = True
        settings["signals"]["lifecycle_pump_signal_min_gain_pct"] = 40.0
        settings["signals"]["lifecycle_route_confirm_bars"] = 1
        engine = SignalEngine(settings)
        engine.ml = DummyLifecycleScorer(probs={"fast_dump": 0.2, "slow_distribution": 0.2, "second_distribution": 0.1, "continuation": 0.2})
        pump = PumpEvent(
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
        candle = Candle("PUMPUSDT", "15m", 2_000, 145.0, 150.0, 140.0, 145.0, 100.0, 901_999, 5000.0, 100)
        engine._append_candle(candle)
        base_row = {"behavior_state": "acceleration", "ctx_high_since_entry": 0.50, "f": 1.0}
        high_row = {
            "behavior_state": "acceleration",
            "ctx_drawdown_from_entry_high": -0.02,
            "orig_ctx_drawdown_from_entry_high": -0.05,
            "ctx_ret_since_entry": 0.10,
            "ret_3": 0.01,
            "f": 1.0,
        }

        with (
            patch("pump_dump_hunter.engine.signal_engine.life.build_lifecycle_row", return_value=base_row),
            patch("pump_dump_hunter.engine.signal_engine.life.build_high_pump_row", return_value=high_row),
        ):
            first = engine._lifecycle_signals(pump, candle)
            second = engine._lifecycle_signals(pump, candle)

        self.assertEqual([a.level for a in first], ["early_alert"])
        self.assertEqual(first[0].model_name, "high_top")
        self.assertEqual(first[0].lifecycle_mode, "high_pump_top")
        self.assertEqual(second, [])

    def test_pump_exhausted_when_price_returns_to_anchor_zone(self):
        settings = temp_settings()
        settings["signals"]["lifecycle_min_remaining_pct"] = 5.0
        settings["signals"]["lifecycle_exhaustion_min_gain_pct"] = 8.0
        engine = SignalEngine(settings)
        pump = PumpEvent(
            event_id="PUMPUSDT-1",
            symbol="PUMPUSDT",
            first_seen=1,
            last_seen=1,
            expires_at=100_000_000,
            trigger_window="1d",
            anchor_price=100.0,
            high_price=125.0,
            high_time=1_000,
            current_price=102.0,
            max_gain_pct=25.0,
        )

        self.assertTrue(engine._lifecycle_pump_exhausted(pump, {"ctx_high_since_entry": 0.25}, remaining=1.96))
        self.assertFalse(engine._lifecycle_pump_exhausted(pump, {"ctx_high_since_entry": 0.25}, remaining=6.0))

    def test_pump_signal_last_time_persists(self):
        settings = temp_settings()
        store = Store(settings["paths"]["db_path"])
        pump = PumpEvent(
            event_id="PUMPUSDT-1",
            symbol="PUMPUSDT",
            first_seen=1,
            last_seen=2,
            expires_at=100_000,
            trigger_window="1d",
            anchor_price=100.0,
            high_price=150.0,
            high_time=1_000,
            current_price=140.0,
            max_gain_pct=50.0,
            short_alerted_after_high_time=1_000,
            short_last_alert_time=42_000,
            short_signal_seq=2,
        )

        store.upsert_pump_events([pump])
        loaded = store.get_pump_event("PUMPUSDT-1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.short_last_alert_time, 42_000)
        self.assertEqual(loaded.short_signal_seq, 2)

    def test_long_last_signal_time_persists_and_history_rows_exist(self):
        settings = temp_settings()
        store = Store(settings["paths"]["db_path"])
        le = LongEvent(
            event_id="LONGUSDT-L-1",
            symbol="LONGUSDT",
            first_seen=1,
            last_seen=2,
            expires_at=100_000,
            entry_price=100.0,
            high_price=110.0,
            current_price=108.0,
            long_signal_seq=2,
            long_last_signal_time=88_000,
            evidence=["test"],
        )

        store.upsert_long_events([le])
        loaded = store.active_long_events(10)
        history = store.long_event_rows()

        self.assertEqual(loaded[0].long_last_signal_time, 88_000)
        self.assertEqual(history[0]["symbol"], "LONGUSDT")
        self.assertEqual(history[0]["long_last_signal_time"], 88_000)

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

    def test_long_timeout_emits_status_alert_and_invalid_is_silent(self):
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
        self.assertEqual([a.level for a in invalid_alerts], [])
        self.assertEqual(engine.long_events_by_symbol["BADUSDT"].status, "closed")
        self.assertEqual(engine.long_events_by_symbol["BADUSDT"].exit_reason, "趋势破坏")

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


class DummyLifecycleScorer:
    lifecycle_ready = True
    lifecycle_router_ready = True

    def __init__(self, probs: dict[str, float] | None = None):
        self.probs = probs or {
            "normal_reversal": 0.02,
            "slow_distribution": 0.02,
            "fast_dump": 0.96,
            "second_distribution": 0.0,
            "continuation": 0.0,
        }

    def lifecycle_columns(self, model_name: str) -> list[str]:
        return ["f"]

    def lifecycle_probabilities(self, feat_row, model_name: str) -> dict[str, float]:
        return self.probs

    def lifecycle_score(self, feat_row, model_name: str) -> float:
        return {
            "fast_top": 0.9,
            "slow_warning": 0.8,
            "fast_short": 0.95,
            "slow_short": 0.7,
            "high_top": 0.9,
            "high_short": 0.1,
        }[model_name]

    def lifecycle_threshold(self, model_name: str) -> float:
        return 0.5


if __name__ == "__main__":
    unittest.main()
