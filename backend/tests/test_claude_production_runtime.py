from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pump_dump_hunter.cli import cmd_monitor
from pump_dump_hunter.board_waterfall import BoardWaterfallEngine
from pump_dump_hunter.waterfall import build_waterfall_engines, validate_waterfall_runtime
from pump_dump_hunter.web import combine_waterfall_accounts
from tests.helpers import temp_settings


class ClaudeProductionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = temp_settings()

    def test_checked_in_production_config_is_claude_only(self) -> None:
        self.assertEqual(self.settings["runtime"]["active_strategy"], "claude_board_wf_1m")
        validate_waterfall_runtime(self.settings)
        _cfg, _board_cfg, engine, extras = build_waterfall_engines(self.settings)
        self.assertIsInstance(engine, BoardWaterfallEngine)
        self.assertEqual(extras, [])
        self.assertFalse(engine._shared)

    def test_rejects_accidental_core5_or_live_execution(self) -> None:
        core = copy.deepcopy(self.settings)
        core["waterfall_quant"]["enabled"] = True
        with self.assertRaisesRegex(RuntimeError, "enabled must be false"):
            validate_waterfall_runtime(core)

        live = copy.deepcopy(self.settings)
        live["waterfall_quant"]["execution_mode"] = "live"
        live["waterfall_quant"]["real_order_enabled"] = True
        with self.assertRaisesRegex(RuntimeError, "paper"):
            validate_waterfall_runtime(live)

    def test_rejects_thin_prewarm_and_unused_micro_stream(self) -> None:
        thin = copy.deepcopy(self.settings)
        thin["waterfall_quant"]["prewarm_limit"] = 1000
        with self.assertRaisesRegex(RuntimeError, "1441"):
            validate_waterfall_runtime(thin)

        micro = copy.deepcopy(self.settings)
        micro["waterfall_quant"]["micro_streams"] = ["aggTrade"]
        with self.assertRaisesRegex(RuntimeError, "unused core5 micro streams"):
            validate_waterfall_runtime(micro)

    def test_monitor_command_dispatches_claude_strategy_to_waterfall_runtime(self) -> None:
        args = SimpleNamespace(
            config=None, settings=None, broad_top=None, discover_every=None,
            samples=0, max_workers=None, top=250,
        )
        with patch("pump_dump_hunter.cli.load_settings", return_value=self.settings), patch(
            "pump_dump_hunter.cli.waterfall_monitor"
        ) as monitor, patch("pump_dump_hunter.cli.asyncio.run") as run:
            self.assertEqual(cmd_monitor(args), 0)
        monitor.assert_called_once()
        run.assert_called_once()
        run.call_args.args[0].close()

    def test_rejects_wrong_account_sizing_contract(self) -> None:
        wrong = copy.deepcopy(self.settings)
        wrong["claude_board_waterfall"]["paper_accounts"][1]["base_margin_fraction"] = 0.2
        with self.assertRaisesRegex(RuntimeError, "claude_fixed10"):
            validate_waterfall_runtime(wrong)

    def test_three_ledgers_do_not_triple_unique_signal_counts(self) -> None:
        accounts = [
            {
                "strategy": "claude_board_wf_1m",
                "paper_initial_balance_usdt": 100.0,
                "paper_equity_usdt": 110.0,
                "paper_realized_pnl_usdt": 10.0,
                "paper_unrealized_pnl_usdt": 0.0,
                "paper_used_margin_usdt": 0.0,
                "paper_free_balance_usdt": 110.0,
            }
            for _ in range(3)
        ]
        out = combine_waterfall_accounts(
            {"open_positions": 1, "closed_positions": 50, "signals": 101, "win_rate": 0.56, "avg_pnl_pct": 0.01},
            accounts,
        )
        self.assertEqual(out["paper_initial_balance_usdt"], 300.0)
        self.assertEqual(out["paper_equity_usdt"], 330.0)
        self.assertEqual(out["closed_positions"], 50)
        self.assertEqual(out["signals"], 101)


if __name__ == "__main__":
    unittest.main()
