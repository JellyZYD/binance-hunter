from __future__ import annotations

import json
import unittest

from pump_dump_hunter.data.websocket_source import parse_combined_kline_message


class WebSocketParserTests(unittest.TestCase):
    def test_unclosed_kline_is_ignored(self):
        raw = json.dumps({"data": {"e": "kline", "k": {"x": False}}})
        self.assertIsNone(parse_combined_kline_message(raw))

    def test_closed_kline_becomes_event(self):
        raw = json.dumps(
            {
                "stream": "pumpusdt@kline_1m",
                "data": {
                    "e": "kline",
                    "k": {
                        "s": "PUMPUSDT",
                        "i": "1m",
                        "t": 1,
                        "T": 60000,
                        "o": "1",
                        "h": "2",
                        "l": "0.8",
                        "c": "1.5",
                        "v": "10",
                        "q": "15",
                        "n": 3,
                        "V": "5",
                        "Q": "7.5",
                        "x": True,
                    },
                },
            }
        )
        event = parse_combined_kline_message(raw)
        self.assertIsNotNone(event)
        self.assertEqual(event.symbol, "PUMPUSDT")
        self.assertEqual(event.candle.close, 1.5)


if __name__ == "__main__":
    unittest.main()
