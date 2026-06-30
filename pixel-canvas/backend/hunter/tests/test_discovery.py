from __future__ import annotations

import unittest

from pump_dump_hunter.discovery import compute_liquidity_records, perpetual_symbols
from pump_dump_hunter.models import SignalParams
from tests.helpers import candle


class DiscoveryTests(unittest.TestCase):
    def test_discovery_uses_only_closed_cutoff(self):
        symbol = "PUMPUSDT"
        start = 1_700_000_000_000
        rows = [candle(symbol, "1m", start + i * 60_000, 100.0, 100.0, 1000.0) for i in range(31)]
        future = candle(symbol, "1m", start + 31 * 60_000, 100.0, 150.0, 999999.0)
        cutoff = rows[-1].close_time
        records = compute_liquidity_records(
            [{"symbol": symbol, "pct_24h": 0.0}],
            {symbol: rows + [future]},
            top_n=1,
            data_cutoff_time=cutoff,
            params=SignalParams(),
        )
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0].pct_15m, 0.0)
        self.assertFalse(records[0].pump_qualified)


    def test_ranked_candidate_still_needs_minimum_gain(self):
        params = SignalParams(volume_ratio_15m=1.5, volume_ratio_30m=1.5, gain_rank_top=5)
        row = {
            "pct_24h": 0.0,
            "pct_15m": 0.8,
            "pct_30m": 1.2,
            "volume_ratio_15m": 5.0,
            "volume_ratio_30m": 5.0,
        }
        from pump_dump_hunter.discovery import is_pump_qualified

        self.assertFalse(is_pump_qualified(row, 1, 1, params))
    def test_symbol_universe_filters_non_ascii_contract_names(self):
        exchange = {
            "symbols": [
                {"symbol": "PUMPUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
                {"symbol": "濡栧竵USDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
            ]
        }
        settings = {
            "universe": {
                "contract_type": "PERPETUAL",
                "quote_asset": "USDT",
                "exclude_symbols": [],
            }
        }
        self.assertEqual(perpetual_symbols(exchange, settings), {"PUMPUSDT"})
if __name__ == "__main__":
    unittest.main()
