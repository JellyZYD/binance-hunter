from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pump_dump_hunter.models import Alert
from pump_dump_hunter.notify import alerts as notify_alerts
from pump_dump_hunter.notify.alerts import AlertSink, render_wecom_markdown


class _FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class NotifyTests(unittest.TestCase):
    def test_wecom_markdown_is_sms_style_without_trade_link(self):
        alert = dummy_alert("short_signal")

        text = render_wecom_markdown(alert)

        self.assertIn("做空", text)
        self.assertIn("PUMPUSDT", text)
        self.assertIn("价格 123.45", text)
        self.assertIn("失效 130", text)
        self.assertIn("置信 高置信 0.910/0.800", text)
        self.assertIn("状态 15m 破位", text)
        self.assertIn("类型 快拉急跌 / fast_short", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("量比", text)
        self.assertNotIn("距锚点", text)

    def test_wecom_skips_non_action_levels(self):
        sink = AlertSink(Path(tempfile.mkdtemp()), webhook_url="https://example.invalid/webhook")
        old_urlopen = notify_alerts.urlopen
        called = {"value": False}
        try:
            def fail_if_called(*_args, **_kwargs):
                called["value"] = True
                raise AssertionError("webhook should not be called")

            notify_alerts.urlopen = fail_if_called

            ok, msg = sink.emit(dummy_alert("long_timeout"))
        finally:
            notify_alerts.urlopen = old_urlopen

        self.assertFalse(ok)
        self.assertEqual(msg, "")
        self.assertFalse(called["value"])

    def test_wecom_nonzero_errcode_is_failure(self):
        sink = AlertSink(Path(tempfile.mkdtemp()), webhook_url="https://example.invalid/webhook")
        old_urlopen = notify_alerts.urlopen
        try:
            notify_alerts.urlopen = lambda *_args, **_kwargs: _FakeResponse('{"errcode":93000,"errmsg":"bad webhook"}')

            ok, msg = sink.push_wecom(dummy_alert("short_signal"))
        finally:
            notify_alerts.urlopen = old_urlopen

        self.assertFalse(ok)
        self.assertIn("errcode=93000", msg)

    def test_wecom_invalid_json_is_failure(self):
        sink = AlertSink(Path(tempfile.mkdtemp()), webhook_url="https://example.invalid/webhook")
        old_urlopen = notify_alerts.urlopen
        try:
            notify_alerts.urlopen = lambda *_args, **_kwargs: _FakeResponse("not-json")

            ok, msg = sink.push_wecom(dummy_alert("short_signal"))
        finally:
            notify_alerts.urlopen = old_urlopen

        self.assertFalse(ok)
        self.assertIn("invalid wecom response", msg)


def dummy_alert(level: str) -> Alert:
    return Alert(
        alert_id=f"PUMPUSDT-{level}-1",
        event_id="PUMPUSDT-1",
        symbol="PUMPUSDT",
        level=level,
        decision_time=1_700_000_000_000,
        source_candle_close_time=1_700_000_000_000,
        data_cutoff_time=1_700_000_000_000,
        price=123.45,
        invalidation_price=130.0,
        anchor_price=100.0,
        high_price=150.0,
        remaining_downside_pct=18.0,
        volume_ratio=2.5,
        evidence=["score=0.910", "threshold=0.800", "tier=high"],
        risks=[],
        category="妖币",
        occurrence=2,
        lifecycle_mode="fast_dump",
        behavior_state="breakdown",
        model_name="fast_short",
        model_score=0.91,
        model_threshold=0.8,
        signal_interval="15m",
    )


if __name__ == "__main__":
    unittest.main()
