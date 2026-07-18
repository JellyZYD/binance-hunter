"""Independent paper-account ledgers driven by one Claude signal stream."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .board_waterfall import STRATEGY_NAME
from .waterfall import WaterfallSignal


DEFAULT_ACCOUNTS = [
    {
        "account_id": "claude_fixed20",
        "label": "Claude·冠军 20%固定",
        "initial_balance_usdt": 100.0,
        "base_margin_fraction": 0.20,
        "leverage": 10.0,
        "sizing_mode": "fixed",
    },
    {
        "account_id": "claude_fixed10",
        "label": "Claude·冠军 10%固定",
        "initial_balance_usdt": 100.0,
        "base_margin_fraction": 0.10,
        "leverage": 10.0,
        "sizing_mode": "fixed",
    },
    {
        "account_id": "claude_drawdown10",
        "label": "Claude·冠军 10%回撤缩仓",
        "initial_balance_usdt": 100.0,
        "base_margin_fraction": 0.10,
        "leverage": 10.0,
        "sizing_mode": "realized_drawdown_ladder",
        "drawdown_ladder": [
            {"below": 0.05, "factor": 1.0},
            {"below": 0.10, "factor": 0.75},
            {"below": 0.15, "factor": 0.50},
            {"below": None, "factor": 0.25},
        ],
    },
]


def paper_account_settings(settings: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    cfg = settings.get("claude_board_waterfall") or {}
    rows = [dict(row) for row in (cfg.get("paper_accounts") or DEFAULT_ACCOUNTS)]
    raw_start = str(cfg.get("paper_account_backfill_from") or "2026-07-13T07:37:00+08:00")
    parsed_start = datetime.fromisoformat(raw_start)
    if parsed_start.tzinfo is None:
        raise ValueError("paper_account_backfill_from must include an explicit timezone")
    start_ms = int(parsed_start.timestamp() * 1000)
    ids = [str(row.get("account_id") or "") for row in rows]
    if len(ids) != len(set(ids)) or any(not account_id for account_id in ids):
        raise ValueError("paper account_id values must be non-empty and unique")
    for row in rows:
        initial = float(row.get("initial_balance_usdt", 0.0))
        fraction = float(row.get("base_margin_fraction", 0.0))
        leverage = float(row.get("leverage", 0.0))
        mode = str(row.get("sizing_mode") or "fixed")
        if initial <= 0 or not 0 < fraction <= 1 or leverage <= 0:
            raise ValueError(f"invalid paper account sizing: {row.get('account_id')}")
        if mode not in {"fixed", "realized_drawdown_ladder"}:
            raise ValueError(f"unknown paper account sizing_mode={mode!r}")
        if mode == "realized_drawdown_ladder":
            _validate_ladder(row.get("drawdown_ladder") or [])
    return rows, start_ms


def _validate_ladder(ladder: list[dict[str, Any]]) -> None:
    if not ladder or ladder[-1].get("below") is not None:
        raise ValueError("drawdown ladder must end with below=null")
    prior = -1.0
    for index, tier in enumerate(ladder):
        factor = float(tier.get("factor", -1.0))
        below = tier.get("below")
        if not 0 <= factor <= 1:
            raise ValueError("drawdown ladder factors must be between 0 and 1")
        if below is not None:
            threshold = float(below)
            if threshold <= prior or threshold <= 0:
                raise ValueError("drawdown ladder thresholds must be positive and increasing")
            prior = threshold
        elif index != len(ladder) - 1:
            raise ValueError("only the final drawdown ladder threshold may be null")


@dataclass
class PaperAccountState:
    account_id: str
    label: str
    initial_balance_usdt: float
    base_margin_fraction: float
    leverage: float
    sizing_mode: str
    drawdown_ladder: list[dict[str, Any]] = field(default_factory=list)
    realized_pnl_usdt: float = 0.0
    peak_equity_usdt: float = 0.0
    max_drawdown_pct: float = 0.0
    open_positions: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def realized_equity_usdt(self) -> float:
        return max(0.0, self.initial_balance_usdt + self.realized_pnl_usdt)

    @property
    def current_drawdown_pct(self) -> float:
        if self.peak_equity_usdt <= 0:
            return 0.0
        return max(0.0, 1.0 - self.realized_equity_usdt / self.peak_equity_usdt)

    def margin_fraction(self) -> float:
        if self.sizing_mode != "realized_drawdown_ladder":
            return self.base_margin_fraction
        drawdown = self.current_drawdown_pct
        for tier in self.drawdown_ladder:
            below = tier.get("below")
            if below is None or drawdown < float(below):
                return self.base_margin_fraction * float(tier.get("factor", 1.0))
        return self.base_margin_fraction

    def account_row(self, backfill_from: int, now_ms: int) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "strategy": STRATEGY_NAME,
            "label": self.label,
            "initial_balance_usdt": self.initial_balance_usdt,
            "base_margin_fraction": self.base_margin_fraction,
            "leverage": self.leverage,
            "sizing_mode": self.sizing_mode,
            "drawdown_ladder": self.drawdown_ladder,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "peak_equity_usdt": self.peak_equity_usdt,
            "max_drawdown_pct": self.max_drawdown_pct,
            "backfill_from": backfill_from,
            "updated_time": now_ms,
        }


class ClaudePaperAccounts:
    """Replays and advances three account books from one master trade path."""

    def __init__(self, store: Any, settings: dict[str, Any]):
        self.store = store
        configs, self.backfill_from = paper_account_settings(settings)
        self.accounts = {row["account_id"]: self._state(row) for row in configs}

    @staticmethod
    def _state(row: dict[str, Any]) -> PaperAccountState:
        initial = float(row.get("initial_balance_usdt", 100.0))
        return PaperAccountState(
            account_id=str(row["account_id"]),
            label=str(row.get("label") or row["account_id"]),
            initial_balance_usdt=initial,
            base_margin_fraction=float(row.get("base_margin_fraction", 0.10)),
            leverage=float(row.get("leverage", 10.0)),
            sizing_mode=str(row.get("sizing_mode") or "fixed"),
            drawdown_ladder=[dict(x) for x in row.get("drawdown_ladder", [])],
            peak_equity_usdt=initial,
        )

    def rebuild(self, master_positions: list[dict[str, Any]]) -> None:
        """Deterministically rebuild every ledger from the shared master history."""
        configs = [
            {
                "account_id": acc.account_id,
                "label": acc.label,
                "initial_balance_usdt": acc.initial_balance_usdt,
                "base_margin_fraction": acc.base_margin_fraction,
                "leverage": acc.leverage,
                "sizing_mode": acc.sizing_mode,
                "drawdown_ladder": acc.drawdown_ladder,
            }
            for acc in self.accounts.values()
        ]
        self.accounts = {row["account_id"]: self._state(row) for row in configs}
        positions: list[dict[str, Any]] = []
        events: list[tuple[int, int, dict[str, Any]]] = []
        for row in master_positions:
            entry_time = int(row.get("entry_time") or 0)
            if entry_time < self.backfill_from:
                continue
            events.append((entry_time, 1, row))
            if str(row.get("status")) == "closed" and int(row.get("exit_time") or 0) > 0:
                events.append((int(row["exit_time"]), 0, row))
        for event_time, kind, master in sorted(events, key=lambda x: (x[0], x[1], str(x[2].get("position_id")))):
            if kind == 1:
                positions.extend(self._open(master, event_time, persist=False))
            else:
                positions.extend(self._close(master, event_time, persist=False))
        now_ms = max([self.backfill_from, *[int(x.get("updated_time") or 0) for x in master_positions]])
        account_rows = [acc.account_row(self.backfill_from, now_ms) for acc in self.accounts.values()]
        final_positions = {
            (row["account_id"], row["master_position_id"]): row
            for row in positions
        }
        self.store.replace_waterfall_paper_accounts(account_rows, list(final_positions.values()))

    def apply_signal(self, signal: WaterfallSignal, master_position: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if signal.strategy != STRATEGY_NAME:
            return []
        master = {
            "position_id": signal.position_id,
            "symbol": signal.symbol,
            "entry_time": signal.decision_time,
            "entry_price": signal.price,
            "exit_time": signal.decision_time,
            "exit_price": signal.price,
            "pnl_pct": signal.pnl_pct,
            "exit_reason": signal.action,
        }
        if master_position:
            master.update(master_position)
        if signal.action == "open_short":
            changed = self._open(master, signal.decision_time, persist=True)
        else:
            changed = self._close(master, signal.decision_time, persist=True)
        updates = [self._update_view(row, signal.action) for row in changed]
        signal.account_updates = updates
        return updates

    def _open(self, master: dict[str, Any], now_ms: int, persist: bool) -> list[dict[str, Any]]:
        changed = []
        master_id = str(master["position_id"])
        for acc in self.accounts.values():
            if master_id in acc.open_positions:
                continue
            equity = acc.realized_equity_usdt
            used_margin = sum(float(p["margin_usdt"]) for p in acc.open_positions.values())
            free = max(0.0, equity - used_margin)
            fraction = acc.margin_fraction()
            if acc.account_id == "claude_fixed20" and float(master.get("margin_usdt") or 0.0) > 0:
                # Preserve the already-running original account exactly during
                # backfill; the two new ledgers are re-sized from its signals.
                margin = float(master["margin_usdt"])
                notional = float(master.get("notional_usdt") or margin * acc.leverage)
                fraction = float(master.get("capital_fraction") or fraction)
            else:
                margin = min(free, equity * fraction)
                notional = margin * acc.leverage
            if margin <= 0 or notional <= 0:
                continue
            row = {
                "account_id": acc.account_id,
                "master_position_id": master_id,
                "symbol": str(master.get("symbol") or ""),
                "status": "open",
                "entry_time": int(master.get("entry_time") or now_ms),
                "entry_price": float(master.get("entry_price") or 0.0),
                "exit_time": None,
                "exit_price": 0.0,
                "exit_reason": "",
                "margin_usdt": margin,
                "notional_usdt": notional,
                "leverage": acc.leverage,
                "sizing_fraction": fraction,
                "drawdown_at_entry": acc.current_drawdown_pct,
                "pnl_pct": 0.0,
                "pnl_usdt": 0.0,
                "equity_before_usdt": equity,
                "equity_after_usdt": equity,
                "updated_time": now_ms,
            }
            acc.open_positions[master_id] = row
            changed.append(row)
        if persist:
            self._persist(changed, now_ms)
        return changed

    def _close(self, master: dict[str, Any], now_ms: int, persist: bool) -> list[dict[str, Any]]:
        changed = []
        master_id = str(master["position_id"])
        for acc in self.accounts.values():
            row = acc.open_positions.pop(master_id, None)
            if row is None:
                continue
            before = acc.realized_equity_usdt
            pnl_pct = float(master.get("pnl_pct") or 0.0)
            if acc.account_id == "claude_fixed20" and master.get("pnl_usdt") is not None:
                pnl = float(master["pnl_usdt"])
            else:
                pnl = float(row["notional_usdt"]) * pnl_pct
            acc.realized_pnl_usdt += pnl
            after = acc.realized_equity_usdt
            acc.peak_equity_usdt = max(acc.peak_equity_usdt, after)
            drawdown = 0.0 if acc.peak_equity_usdt <= 0 else max(0.0, 1.0 - after / acc.peak_equity_usdt)
            acc.max_drawdown_pct = max(acc.max_drawdown_pct, drawdown)
            row.update({
                "status": "closed",
                "exit_time": int(master.get("exit_time") or now_ms),
                "exit_price": float(master.get("exit_price") or 0.0),
                "exit_reason": str(master.get("exit_reason") or ""),
                "pnl_pct": pnl_pct,
                "pnl_usdt": pnl,
                "equity_before_usdt": before,
                "equity_after_usdt": after,
                "updated_time": now_ms,
            })
            changed.append(row)
        if persist:
            self._persist(changed, now_ms)
        return changed

    def _persist(self, changed: list[dict[str, Any]], now_ms: int) -> None:
        accounts = [acc.account_row(self.backfill_from, now_ms) for acc in self.accounts.values()]
        self.store.upsert_waterfall_paper_state(accounts, changed)

    def _update_view(self, row: dict[str, Any], action: str) -> dict[str, Any]:
        acc = self.accounts[row["account_id"]]
        return {
            "account_id": acc.account_id,
            "label": acc.label,
            "action": action,
            "equity_usdt": acc.realized_equity_usdt,
            "pnl_usdt": float(row.get("pnl_usdt") or 0.0),
            "margin_usdt": float(row.get("margin_usdt") or 0.0),
            "notional_usdt": float(row.get("notional_usdt") or 0.0),
            "sizing_fraction": float(row.get("sizing_fraction") or 0.0),
            "current_drawdown_pct": acc.current_drawdown_pct,
            "max_drawdown_pct": acc.max_drawdown_pct,
        }
