from __future__ import annotations

import unittest

from pump_dump_hunter.data.store import Store
from pump_dump_hunter.paper_accounts import ClaudePaperAccounts
from pump_dump_hunter.waterfall import WaterfallSignal, render_waterfall_wecom
from tests.helpers import temp_settings


class ClaudePaperAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = temp_settings()

    @staticmethod
    def master(position_id: str, entry_time: int, pnl_pct: float) -> dict:
        return {
            "position_id": position_id,
            "symbol": f"{position_id}USDT",
            "strategy": "claude_board_wf_1m",
            "status": "closed",
            "entry_time": entry_time,
            "entry_price": 1.0,
            "exit_time": entry_time + 60_000,
            "exit_price": 1.0 - pnl_pct,
            "exit_reason": "test",
            "pnl_pct": pnl_pct,
            "updated_time": entry_time + 60_000,
        }

    def test_backfill_replays_same_signals_with_independent_sizing(self) -> None:
        self.settings["claude_board_waterfall"]["paper_account_backfill_from"] = "2023-07-13T07:37:00+08:00"
        store = Store(self.settings["paths"]["db_path"])
        manager = ClaudePaperAccounts(store, self.settings)
        start = manager.backfill_from + 60_000
        rows = [
            self.master("A", start, 0.10),
            self.master("B", start + 120_000, -0.06),
            self.master("C", start + 240_000, 0.02),
        ]
        manager.rebuild(rows)

        summaries = {row["account_id"]: row for row in store.waterfall_paper_account_summaries()}
        self.assertEqual(set(summaries), {"claude_fixed20", "claude_fixed10", "claude_drawdown10"})
        self.assertEqual({row["closed_positions"] for row in summaries.values()}, {3})
        self.assertGreater(summaries["claude_fixed20"]["paper_equity_usdt"], 100.0)
        self.assertGreater(summaries["claude_fixed10"]["paper_equity_usdt"], 100.0)

        ladder_rows = store.waterfall_account_position_rows("claude_drawdown10", limit=10)
        third = next(row for row in ladder_rows if row["master_position_id"] == "C")
        self.assertAlmostEqual(third["drawdown_at_entry"], 0.06, places=6)
        self.assertAlmostEqual(third["sizing_fraction"], 0.075, places=6)

        # Rebuilding the same history is idempotent.
        before = summaries
        manager.rebuild(rows)
        after = {row["account_id"]: row for row in store.waterfall_paper_account_summaries()}
        for account_id in before:
            self.assertAlmostEqual(after[account_id]["paper_equity_usdt"], before[account_id]["paper_equity_usdt"])
            self.assertEqual(after[account_id]["closed_positions"], 3)

    def test_one_master_signal_updates_three_accounts_and_one_message(self) -> None:
        store = Store(self.settings["paths"]["db_path"])
        manager = ClaudePaperAccounts(store, self.settings)
        manager.rebuild([])
        signal = WaterfallSignal(
            signal_id="sig-1",
            position_id="pos-1",
            symbol="ALTUSDT",
            strategy="claude_board_wf_1m",
            action="open_short",
            family="board_waterfall",
            rule="board40_drop7_60m",
            decision_time=manager.backfill_from + 60_000,
            price=1.0,
            stop_price=1.02,
        )
        updates = manager.apply_signal(signal)
        self.assertEqual(len(updates), 3)
        self.assertEqual(len(signal.account_updates), 3)
        text = render_waterfall_wecom(signal)
        self.assertEqual(text.count("Claude·冠军"), 4)  # title plus three account lines
        self.assertIn("20%固定", text)
        self.assertIn("10%固定", text)
        self.assertIn("10%回撤缩仓", text)

    def test_original_20pct_account_preserves_master_historical_sizing(self) -> None:
        store = Store(self.settings["paths"]["db_path"])
        manager = ClaudePaperAccounts(store, self.settings)
        row = self.master("MASTER", manager.backfill_from + 60_000, 0.10)
        row.update({"margin_usdt": 17.25, "notional_usdt": 172.5, "capital_fraction": 0.20})
        manager.rebuild([row])
        account_rows = store.waterfall_account_position_rows("claude_fixed20")
        self.assertEqual(len(account_rows), 1)
        self.assertAlmostEqual(account_rows[0]["margin_usdt"], 17.25)
        self.assertAlmostEqual(account_rows[0]["notional_usdt"], 172.5)
        summary = next(
            x for x in store.waterfall_paper_account_summaries()
            if x["account_id"] == "claude_fixed20"
        )
        self.assertAlmostEqual(summary["paper_equity_usdt"], 117.25)

    def test_bankrupt_account_does_not_create_zero_notional_followup(self) -> None:
        store = Store(self.settings["paths"]["db_path"])
        manager = ClaudePaperAccounts(store, self.settings)
        first = self.master("LOSS", manager.backfill_from + 60_000, -1.0)
        second = self.master("AFTER", manager.backfill_from + 180_000, 0.50)
        manager.rebuild([first, second])
        for account_id in ("claude_fixed20", "claude_fixed10", "claude_drawdown10"):
            rows = store.waterfall_account_position_rows(account_id)
            self.assertEqual([row["master_position_id"] for row in rows], ["LOSS"])
            self.assertEqual(rows[0]["notional_usdt"] > 0, True)

    def test_open_position_restart_rebuild_then_exit_is_exactly_once(self) -> None:
        store = Store(self.settings["paths"]["db_path"])
        manager = ClaudePaperAccounts(store, self.settings)
        opened = {
            "position_id": "OPEN",
            "symbol": "OPENUSDT",
            "strategy": "claude_board_wf_1m",
            "status": "open",
            "entry_time": manager.backfill_from + 60_000,
            "entry_price": 1.0,
            "margin_usdt": 20.0,
            "notional_usdt": 200.0,
            "capital_fraction": 0.2,
            "updated_time": manager.backfill_from + 60_000,
        }
        manager.rebuild([opened])
        restarted = ClaudePaperAccounts(store, self.settings)
        restarted.rebuild([opened])
        for account_id in restarted.accounts:
            rows = store.waterfall_account_position_rows(account_id, status="open")
            self.assertEqual(len(rows), 1)

        exit_signal = WaterfallSignal(
            signal_id="exit-open",
            position_id="OPEN",
            symbol="OPENUSDT",
            strategy="claude_board_wf_1m",
            action="take_profit",
            family="board_waterfall",
            rule="board40_drop7_60m",
            decision_time=manager.backfill_from + 120_000,
            price=0.95,
            stop_price=1.02,
            pnl_pct=0.05,
        )
        updates = restarted.apply_signal(exit_signal, {**opened, "status": "closed", "exit_time": exit_signal.decision_time, "exit_price": 0.95, "pnl_pct": 0.05, "pnl_usdt": 10.0})
        self.assertEqual(len(updates), 3)
        for account_id in restarted.accounts:
            rows = store.waterfall_account_position_rows(account_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
