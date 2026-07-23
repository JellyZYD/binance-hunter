from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..data.store import Store
from ..waterfall import WaterfallSignal


@dataclass(frozen=True, order=True)
class SignalCursor:
    sequence: int = 0
    decision_time: int = 0
    signal_id: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "sequence": int(self.sequence),
                "decision_time": int(self.decision_time),
                "signal_id": self.signal_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "SignalCursor":
        if not raw:
            return cls()
        try:
            row = json.loads(raw)
            return cls(
                int(row.get("sequence") or 0),
                int(row.get("decision_time") or 0),
                str(row.get("signal_id") or ""),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls()


def waterfall_signal_from_row(row: dict[str, Any]) -> WaterfallSignal:
    evidence = row.get("evidence")
    if evidence is None:
        try:
            evidence = json.loads(str(row.get("evidence_json") or "[]"))
        except json.JSONDecodeError:
            evidence = []
    return WaterfallSignal(
        signal_id=str(row["signal_id"]),
        position_id=str(row["position_id"]),
        symbol=str(row["symbol"]),
        strategy=str(row["strategy"]),
        action=str(row["action"]),
        family=str(row["family"]),
        rule=str(row["rule"]),
        decision_time=int(row["decision_time"]),
        price=float(row["price"]),
        stop_price=float(row["stop_price"]),
        pnl_pct=float(row.get("pnl_pct") or 0.0),
        confidence=float(row.get("confidence") or 0.0),
        tier=str(row.get("tier") or "normal"),
        notional_usdt=float(row.get("notional_usdt") or 0.0),
        margin_usdt=float(row.get("margin_usdt") or 0.0),
        leverage=float(row.get("leverage") or 1.0),
        account_equity_usdt=float(row.get("account_equity_usdt") or 0.0),
        evidence=[str(item) for item in (evidence or [])],
    )


class SharedPaperSignalSource:
    """Read the paper monitor's durable signal outbox without another market feed."""

    def __init__(self, db_path: str | Path, strategy: str):
        self.db_path = Path(db_path).resolve()
        self.strategy = str(strategy)
        # Run migrations before opening the long-lived query-only connection.
        Store(self.db_path)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            # This reader runs in the live execution event loop. WAL normally
            # makes reads immediate; if a lock still occurs, fail quickly and
            # let the service retry without starving private order events.
            conn = sqlite3.connect(self.db_path, timeout=0.5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=250")
            conn.execute("PRAGMA query_only=ON")
            self._conn = conn
        return self._conn

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        last_error: sqlite3.Error | None = None
        for _attempt in range(2):
            try:
                return list(self._connect().execute(sql, params).fetchall())
            except sqlite3.Error as exc:
                last_error = exc
                self.close()
        assert last_error is not None
        raise last_error

    def latest_cursor(self) -> SignalCursor:
        rows = self._query(
            """SELECT seq,decision_time,signal_id FROM waterfall_signal_outbox
            WHERE strategy=? ORDER BY seq DESC LIMIT 1""",
            (self.strategy,),
        )
        if not rows:
            return SignalCursor()
        return SignalCursor(
            int(rows[0]["seq"]),
            int(rows[0]["decision_time"]),
            str(rows[0]["signal_id"]),
        )

    def signals_after(
        self,
        cursor: SignalCursor,
        limit: int = 100,
    ) -> list[tuple[int, WaterfallSignal]]:
        rows = self._query(
            """SELECT o.seq,s.* FROM waterfall_signal_outbox o
            JOIN waterfall_signals s ON s.signal_id=o.signal_id
            WHERE o.strategy=? AND o.seq>?
            ORDER BY o.seq ASC LIMIT ?""",
            (
                self.strategy,
                int(cursor.sequence),
                int(limit),
            ),
        )
        return [
            (int(row["seq"]), waterfall_signal_from_row(dict(row)))
            for row in rows
        ]

    def protection_states(self) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT strategy,symbol,position_id,decision_time,trail_price,
            arm_trail,flow_hold_through,updated_time
            FROM waterfall_protection_state
            WHERE strategy=? ORDER BY symbol""",
            (self.strategy,),
        )
        return [
            {
                **dict(row),
                "arm_trail": bool(row["arm_trail"]),
                "flow_hold_through": bool(row["flow_hold_through"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
