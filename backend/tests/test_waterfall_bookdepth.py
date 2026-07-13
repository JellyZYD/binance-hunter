from __future__ import annotations

import json
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from pump_dump_hunter.board_waterfall import BoardWaterfallEngine
from pump_dump_hunter.depth_cache import DepthSignalCache, DepthSnapshotPublisher
from pump_dump_hunter.micro_collector import balanced_depth_pool
from pump_dump_hunter.models import Candle, KlineClosed
from pump_dump_hunter.waterfall import WaterfallEngine, render_waterfall_wecom

from .helpers import temp_settings


class DepthCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = temp_settings()
        self.path = Path(self.settings["_tmp_root"]) / "micro" / "latest_depth.json"

    def test_publisher_builds_two_minute_imbalance_delta(self) -> None:
        publisher = DepthSnapshotPublisher(self.path)
        start = 1_700_000_000_000
        publisher.add(self._row(start, 40.0, 60.0))
        publisher.add(self._row(start + 60_000, 50.0, 50.0))
        latest = publisher.add(self._row(start + 120_000, 70.0, 30.0))
        publisher.flush(start + 120_000)

        self.assertIsNotNone(latest)
        self.assertAlmostEqual(float(latest["imbalance_delta_2m"]), 0.6)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("ALTUSDT", payload["symbols"])

    def test_reader_requires_fresh_snapshot_and_valid_baseline(self) -> None:
        publisher = DepthSnapshotPublisher(self.path)
        start = 1_700_000_000_000
        publisher.add(self._row(start, 40.0, 60.0))
        publisher.add(self._row(start + 120_000, 70.0, 30.0))
        publisher.flush(start + 120_000)
        cache = DepthSignalCache(self.path)

        accepted = cache.decision("ALTUSDT", start + 150_000)
        self.assertTrue(accepted["available"])
        self.assertTrue(accepted["ok"])
        stale = cache.decision("ALTUSDT", start + 220_000)
        self.assertFalse(stale["available"])
        self.assertEqual(stale["reason"], "bookdepth_stale")

    def test_reader_rejects_weaker_bid_imbalance_without_blocking_base_engine(self) -> None:
        publisher = DepthSnapshotPublisher(self.path)
        start = 1_700_000_000_000
        publisher.add(self._row(start, 70.0, 30.0))
        publisher.add(self._row(start + 120_000, 40.0, 60.0))
        publisher.flush(start + 120_000)
        decision = DepthSignalCache(self.path).decision("ALTUSDT", start + 130_000)
        self.assertTrue(decision["available"])
        self.assertFalse(decision["ok"])
        self.assertEqual(decision["reason"], "bookdepth_imbalance_rejected")

    @staticmethod
    def _row(ts: int, bid: float, ask: float) -> dict:
        return {
            "ts": ts,
            "symbol": "ALTUSDT",
            "bid_notional20": bid,
            "ask_notional20": ask,
        }

    def test_depth_pool_covers_gainers_losers_and_liquid_contracts(self) -> None:
        tickers = [
            {
                "symbol": f"ALT{i}USDT",
                "priceChangePercent": str(i - 5),
                "quoteVolume": str((10 - i) * 1000),
            }
            for i in range(10)
        ]
        eligible = [str(row["symbol"]) for row in tickers]
        selected = balanced_depth_pool(tickers, eligible, 6)
        self.assertEqual(len(selected), 6)
        self.assertIn("ALT9USDT", selected)
        self.assertIn("ALT0USDT", selected)
        self.assertIn("ALT1USDT", selected)


class WaterfallBookDepthIntegrationTests(unittest.TestCase):
    def test_enhanced_entry_uses_same_core_account_and_is_labeled(self) -> None:
        settings = temp_settings()
        engine = WaterfallEngine(settings)
        now = 1_700_000_180_000
        candle = Candle(
            symbol="ALTUSDT",
            interval="1m",
            open_time=now - 60_000,
            close_time=now - 1,
            open=100.0,
            high=100.0,
            low=94.0,
            close=95.0,
            volume=1000.0,
            quote_volume=500_000.0,
            trades=1000,
            taker_buy_base=300.0,
            taker_buy_quote=150_000.0,
        )
        engine.candles["ALTUSDT"] = deque([candle], maxlen=engine.maxlen)
        feat = {
            "qv30": 2_000_000.0,
            "volr20": 5.0,
            "volr5_20": 3.0,
            "tsell": 0.70,
            "drop_5m": 0.06,
            "dd_from_24h_high": 0.12,
            "prior_body_low_8": 99.0,
            "prior_body_low_20": 99.0,
            "prior_body_low_40": 99.0,
        }
        engine.micro.features = lambda *_args: {}
        engine.micro_signal_decision = lambda *_args: {
            "ok": True,
            "tier": "strong",
            "reason": "test_agg",
            "confidence_boost": 0.12,
        }
        engine.depth_cache.decision = lambda *_args: {
            "available": True,
            "ok": True,
            "reason": "bookdepth_imbalance_confirmed",
            "age_seconds": 8.0,
            "baseline_age_seconds": 120.0,
            "imbalance20": 0.25,
            "baseline_imbalance20": -0.10,
            "imbalance_delta_2m": 0.35,
        }

        with patch("pump_dump_hunter.waterfall.classify_family", return_value="post_pump"), patch(
            "pump_dump_hunter.waterfall.signal_ok", return_value=True
        ), patch("pump_dump_hunter.waterfall.utc_ms", return_value=now):
            result = engine.entry_signal("ALTUSDT", feat, candle)

        self.assertIsNotNone(result)
        position, signal = result
        self.assertEqual(position.strategy, engine.strategy)
        self.assertEqual(signal.strategy, engine.strategy)
        self.assertEqual(signal.tier, "bookdepth_strong")
        self.assertIn("bookdepth=bookdepth_imbalance_confirmed", signal.evidence)
        self.assertIn("BookDepth增强", render_waterfall_wecom(signal))


class DepthCacheDirectionTests(unittest.TestCase):
    """The champion board label confirms on the OPPOSITE sign to codex: it wants
    the near book NOT stacking bids (ask-heavy), because near-bid laddering is a
    knife-catch/bounce and a worse short (train PF 1.40 / verdict PF 1.49)."""

    def setUp(self) -> None:
        self.settings = temp_settings()
        self.path = Path(self.settings["_tmp_root"]) / "micro" / "latest_depth.json"

    def _publish(self, bid0: float, ask0: float, bid1: float, ask1: float) -> int:
        publisher = DepthSnapshotPublisher(self.path)
        start = 1_700_000_000_000
        publisher.add({"ts": start, "symbol": "ALTUSDT", "bid_notional20": bid0, "ask_notional20": ask0})
        publisher.add({"ts": start + 120_000, "symbol": "ALTUSDT", "bid_notional20": bid1, "ask_notional20": ask1})
        publisher.flush(start + 120_000)
        return start + 130_000

    def test_ask_heavy_and_bid_heavy_read_the_same_data_with_opposite_signs(self) -> None:
        # near book shifts ask-heavy (bid share falls): delta < 0.
        now = self._publish(70.0, 30.0, 40.0, 60.0)
        board = DepthSignalCache(self.path, confirm_direction="ask_heavy").decision("ALTUSDT", now)
        codex = DepthSignalCache(self.path, confirm_direction="bid_heavy").decision("ALTUSDT", now)
        self.assertTrue(board["available"])
        self.assertTrue(board["ok"])  # champion: sellers dominate -> confirm
        self.assertTrue(codex["available"])
        self.assertFalse(codex["ok"])  # codex: not more bid-heavy -> reject
        self.assertLess(board["imbalance_delta_2m"], 0.0)

    def test_ask_heavy_rejects_when_near_book_stacks_bids(self) -> None:
        # near book shifts bid-heavy (bid share rises): delta > 0 -> bounce risk.
        now = self._publish(30.0, 70.0, 60.0, 40.0)
        board = DepthSignalCache(self.path, confirm_direction="ask_heavy").decision("ALTUSDT", now)
        self.assertTrue(board["available"])
        self.assertFalse(board["ok"])
        self.assertGreater(board["imbalance_delta_2m"], 0.0)


class BoardBookDepthTierTests(unittest.TestCase):
    START = 1_700_000_000_000

    def _board_history(self) -> list[Candle]:
        history: list[Candle] = []
        for i in range(1381):
            history.append(self._c(self.START + i * 60_000, 100.0, 100.0, 10_000.0))
        for i in range(59):
            price = 100.0 + 60.0 * (i + 1) / 59.0
            history.append(self._c(self.START + (1381 + i) * 60_000, price, price, 10_000.0))
        return history

    def _trigger(self) -> KlineClosed:
        candle = self._c(self.START + 1440 * 60_000, 160.0, 148.0, 20_000.0, high=160.0, low=147.0)
        return KlineClosed("BOARDUSDT", "1m", candle)

    def _engine(self, extra: dict | None = None) -> BoardWaterfallEngine:
        settings = temp_settings()
        if extra:
            settings["claude_board_waterfall"] = extra
        engine = BoardWaterfallEngine(settings)
        engine.prime_candles(self._board_history())
        return engine

    def test_ask_heavy_confirmation_tags_and_boosts_without_filtering(self) -> None:
        engine = self._engine()
        engine.depth_cache.decision = lambda *_a: {
            "available": True, "ok": True, "reason": "bookdepth_imbalance_confirmed",
            "age_seconds": 8.0, "imbalance20": -0.30, "baseline_imbalance20": 0.10,
            "imbalance_delta_2m": -0.40,
        }
        _, _, signals = engine.on_kline(self._trigger())
        self.assertEqual([s.action for s in signals], ["open_short"])
        sig = signals[0]
        self.assertEqual(sig.tier, "depth_confirmed")
        self.assertAlmostEqual(sig.confidence, 0.77)
        self.assertIn("bookdepth=bookdepth_imbalance_confirmed", sig.evidence)
        self.assertIn("深度确认", render_waterfall_wecom(sig))

    def test_unconfirmed_depth_still_opens_at_base_tier(self) -> None:
        engine = self._engine()
        engine.depth_cache.decision = lambda *_a: {
            "available": True, "ok": False, "reason": "bookdepth_imbalance_rejected",
            "age_seconds": 8.0, "imbalance20": 0.30, "baseline_imbalance20": -0.10,
            "imbalance_delta_2m": 0.40,
        }
        _, _, signals = engine.on_kline(self._trigger())
        self.assertEqual([s.action for s in signals], ["open_short"])
        self.assertEqual(signals[0].tier, "normal")
        self.assertAlmostEqual(signals[0].confidence, 0.67)

    def test_filter_mode_blocks_unconfirmed_but_keeps_fail_open_on_missing(self) -> None:
        engine = self._engine({"bookdepth_filter_mode": True})
        engine.depth_cache.decision = lambda *_a: {
            "available": True, "ok": False, "reason": "bookdepth_imbalance_rejected",
        }
        _, _, signals = engine.on_kline(self._trigger())
        self.assertEqual(signals, [])  # hard filter rejects an unconfirmed entry

        engine2 = self._engine({"bookdepth_filter_mode": True})
        engine2.depth_cache.decision = lambda *_a: {"available": False, "ok": False, "reason": "bookdepth_missing"}
        _, _, signals2 = engine2.on_kline(self._trigger())
        self.assertEqual([s.action for s in signals2], ["open_short"])  # missing depth never blocks

    def test_missing_cache_defaults_to_fail_open_base_tier(self) -> None:
        engine = self._engine()  # no latest_depth.json on disk
        _, _, signals = engine.on_kline(self._trigger())
        self.assertEqual([s.action for s in signals], ["open_short"])
        self.assertEqual(signals[0].tier, "normal")

    @staticmethod
    def _c(open_time: int, open_: float, close: float, quote_volume: float,
           *, high: float | None = None, low: float | None = None) -> Candle:
        return Candle(
            symbol="BOARDUSDT", interval="1m", open_time=open_time, open=open_,
            high=max(open_, close) if high is None else high,
            low=min(open_, close) if low is None else low,
            close=close, volume=100.0, close_time=open_time + 59_999,
            quote_volume=quote_volume, trades=100, taker_buy_base=40.0,
            taker_buy_quote=quote_volume * 0.4,
        )


if __name__ == "__main__":
    unittest.main()
