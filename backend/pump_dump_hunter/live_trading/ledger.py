from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import AccountSnapshot, LiveFill, LiveOrder, LivePosition, TradeIntent


class LiveLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_intents(
                    intent_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decision_time INTEGER NOT NULL,
                    signal_price TEXT NOT NULL,
                    strategy_stop_price TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_orders(
                    client_order_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    execution_policy TEXT NOT NULL,
                    state TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    reduce_only INTEGER NOT NULL,
                    exchange_order_id INTEGER,
                    filled_quantity TEXT NOT NULL,
                    applied_quantity TEXT NOT NULL DEFAULT '0',
                    applied_notional TEXT NOT NULL DEFAULT '0',
                    average_price TEXT NOT NULL,
                    reference_price TEXT NOT NULL DEFAULT '0',
                    arrival_price TEXT NOT NULL DEFAULT '0',
                    created_time INTEGER NOT NULL,
                    submit_time INTEGER NOT NULL DEFAULT 0,
                    ack_time INTEGER NOT NULL DEFAULT 0,
                    first_fill_time INTEGER NOT NULL DEFAULT 0,
                    final_fill_time INTEGER NOT NULL DEFAULT 0,
                    slippage_bps TEXT NOT NULL DEFAULT '0',
                    arrival_slippage_bps TEXT NOT NULL DEFAULT '0',
                    updated_time INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    error_message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_orders_intent ON live_orders(intent_id, updated_time);
                CREATE INDEX IF NOT EXISTS idx_live_orders_exchange ON live_orders(exchange_order_id);

                CREATE TABLE IF NOT EXISTS live_fills(
                    exchange_order_id INTEGER NOT NULL,
                    trade_id INTEGER NOT NULL,
                    client_order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    commission TEXT NOT NULL,
                    commission_asset TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    maker INTEGER NOT NULL,
                    trade_time INTEGER NOT NULL,
                    PRIMARY KEY(exchange_order_id, trade_id)
                );

                CREATE TABLE IF NOT EXISTS live_positions(
                    position_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    structure_stop_price TEXT NOT NULL,
                    trail_price TEXT NOT NULL,
                    liquidation_price TEXT NOT NULL,
                    entry_time INTEGER NOT NULL,
                    exit_time INTEGER NOT NULL,
                    exit_price TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    entry_client_order_id TEXT NOT NULL,
                    structure_algo_id INTEGER,
                    structure_client_algo_id TEXT NOT NULL,
                    trail_algo_id INTEGER,
                    trail_client_algo_id TEXT NOT NULL,
                    protected INTEGER NOT NULL,
                    updated_time INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_positions_status ON live_positions(status, updated_time);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_live_positions_open_symbol
                    ON live_positions(symbol) WHERE status IN ('open','closing');

                CREATE TABLE IF NOT EXISTS live_algo_orders(
                    client_algo_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    role TEXT NOT NULL,
                    algo_id INTEGER,
                    status TEXT NOT NULL,
                    trigger_price TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_time INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_account_snapshots(
                    snapshot_time INTEGER PRIMARY KEY,
                    wallet_balance TEXT NOT NULL,
                    available_balance TEXT NOT NULL,
                    margin_balance TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    total_maintenance_margin TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_income(
                    tran_id INTEGER PRIMARY KEY,
                    income_type TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    income_time INTEGER NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_income_time ON live_income(income_time, income_type);

                CREATE TABLE IF NOT EXISTS live_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_events_time ON live_events(event_time, id);

                CREATE TABLE IF NOT EXISTS live_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_time INTEGER NOT NULL
                );
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(live_orders)")}
            if "applied_quantity" not in columns:
                conn.execute("ALTER TABLE live_orders ADD COLUMN applied_quantity TEXT NOT NULL DEFAULT '0'")
            if "applied_notional" not in columns:
                conn.execute("ALTER TABLE live_orders ADD COLUMN applied_notional TEXT NOT NULL DEFAULT '0'")
            order_migrations = {
                "reference_price": "TEXT NOT NULL DEFAULT '0'",
                "arrival_price": "TEXT NOT NULL DEFAULT '0'",
                "submit_time": "INTEGER NOT NULL DEFAULT 0",
                "ack_time": "INTEGER NOT NULL DEFAULT 0",
                "first_fill_time": "INTEGER NOT NULL DEFAULT 0",
                "final_fill_time": "INTEGER NOT NULL DEFAULT 0",
                "slippage_bps": "TEXT NOT NULL DEFAULT '0'",
                "arrival_slippage_bps": "TEXT NOT NULL DEFAULT '0'",
            }
            for name, definition in order_migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE live_orders ADD COLUMN {name} {definition}")
            conn.execute("DROP INDEX IF EXISTS idx_live_positions_open_symbol")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_live_positions_open_symbol "
                "ON live_positions(symbol) WHERE status IN ('open','closing')"
            )

    def save_intent(self, intent: TradeIntent) -> bool:
        row = intent.to_dict()
        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO live_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["intent_id"], row["signal_id"], row["position_id"], row["strategy"],
                    row["symbol"], row["action"], row["decision_time"], row["signal_price"],
                    row["strategy_stop_price"], row["reason"], json.dumps(row["evidence"], ensure_ascii=False),
                ),
            )
            return cur.rowcount > 0

    def save_order(self, order: LiveOrder) -> None:
        row = order.to_dict()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO live_orders(
                    client_order_id,intent_id,symbol,side,order_type,execution_policy,state,
                    quantity,price,reduce_only,exchange_order_id,filled_quantity,applied_quantity,
                    applied_notional,average_price,reference_price,arrival_price,created_time,submit_time,ack_time,
                    first_fill_time,final_fill_time,slippage_bps,arrival_slippage_bps,
                    updated_time,error_code,error_message
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    state=excluded.state, exchange_order_id=excluded.exchange_order_id,
                    filled_quantity=excluded.filled_quantity, applied_quantity=excluded.applied_quantity,
                    applied_notional=excluded.applied_notional,
                    average_price=excluded.average_price, reference_price=excluded.reference_price,
                    arrival_price=excluded.arrival_price,
                    submit_time=excluded.submit_time, ack_time=excluded.ack_time,
                    first_fill_time=excluded.first_fill_time, final_fill_time=excluded.final_fill_time,
                    slippage_bps=excluded.slippage_bps,
                    arrival_slippage_bps=excluded.arrival_slippage_bps,
                    updated_time=excluded.updated_time, error_code=excluded.error_code,
                    error_message=excluded.error_message
                """,
                (
                    row["client_order_id"], row["intent_id"], row["symbol"], row["side"],
                    row["order_type"], row["execution_policy"], row["state"], row["quantity"],
                    row["price"], int(row["reduce_only"]), row["exchange_order_id"],
                    row["filled_quantity"], row["applied_quantity"], row["applied_notional"],
                    row["average_price"], row["reference_price"], row["arrival_price"], row["created_time"],
                    row["submit_time"], row["ack_time"], row["first_fill_time"],
                    row["final_fill_time"], row["slippage_bps"], row["arrival_slippage_bps"],
                    row["updated_time"], row["error_code"], row["error_message"],
                ),
            )

    def save_fill(self, fill: LiveFill) -> bool:
        row = fill.to_dict()
        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO live_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["exchange_order_id"], row["trade_id"], row["client_order_id"], row["symbol"],
                    row["side"], row["quantity"], row["price"], row["commission"],
                    row["commission_asset"], row["realized_pnl"], int(row["maker"]), row["trade_time"],
                ),
            )
            return cur.rowcount > 0

    def save_position(self, position: LivePosition) -> None:
        row = position.to_dict()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO live_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(position_id) DO UPDATE SET
                    status=excluded.status, quantity=excluded.quantity, entry_price=excluded.entry_price,
                    structure_stop_price=excluded.structure_stop_price, trail_price=excluded.trail_price,
                    liquidation_price=excluded.liquidation_price, exit_time=excluded.exit_time,
                    exit_price=excluded.exit_price, realized_pnl=excluded.realized_pnl,
                    structure_algo_id=excluded.structure_algo_id,
                    structure_client_algo_id=excluded.structure_client_algo_id,
                    trail_algo_id=excluded.trail_algo_id, trail_client_algo_id=excluded.trail_client_algo_id,
                    protected=excluded.protected, updated_time=excluded.updated_time,
                    metadata_json=excluded.metadata_json
                """,
                (
                    row["position_id"], row["intent_id"], row["symbol"], row["status"], row["quantity"],
                    row["entry_price"], row["structure_stop_price"], row["trail_price"],
                    row["liquidation_price"], row["entry_time"], row["exit_time"], row["exit_price"],
                    row["realized_pnl"], row["entry_client_order_id"], row["structure_algo_id"],
                    row["structure_client_algo_id"], row["trail_algo_id"], row["trail_client_algo_id"],
                    int(row["protected"]), row["updated_time"], json.dumps(row["metadata"], ensure_ascii=False),
                ),
            )

    def save_algo(self, row: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO live_algo_orders VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(client_algo_id) DO UPDATE SET
                    algo_id=excluded.algo_id, status=excluded.status,
                    trigger_price=excluded.trigger_price, raw_json=excluded.raw_json,
                    updated_time=excluded.updated_time
                """,
                (
                    row["client_algo_id"], row["position_id"], row["symbol"], row["role"],
                    row.get("algo_id"), row["status"], str(row.get("trigger_price") or "0"),
                    json.dumps(row.get("raw") or {}, ensure_ascii=False, separators=(",", ":")),
                    int(row["updated_time"]),
                ),
            )

    def save_account_snapshot(self, snapshot: AccountSnapshot, raw: dict[str, Any] | None = None) -> None:
        row = snapshot.to_dict()
        with self.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO live_account_snapshots VALUES(?,?,?,?,?,?,?)",
                (
                    row["snapshot_time"], row["wallet_balance"], row["available_balance"],
                    row["margin_balance"], row["unrealized_pnl"], row["total_maintenance_margin"],
                    json.dumps(raw or {}, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def append_event(self, event_time: int, event_type: str, correlation_id: str, payload: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO live_events(event_time,event_type,correlation_id,payload_json) VALUES(?,?,?,?)",
                (event_time, event_type, correlation_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )

    def open_positions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM live_positions WHERE status IN ('open','closing') ORDER BY entry_time"
                )
            ]

    def order(self, client_order_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM live_orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            return dict(row) if row else None

    def pending_orders(self) -> list[dict[str, Any]]:
        terminal = ("FILLED", "CANCELLED", "EXPIRED", "EXCHANGE_REJECTED", "RISK_REJECTED", "CLOSED")
        placeholders = ",".join("?" for _ in terminal)
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM live_orders
                    WHERE state NOT IN ({placeholders}) OR CAST(filled_quantity AS REAL) > CAST(applied_quantity AS REAL)
                    ORDER BY created_time""",
                    terminal,
                )
            ]

    def intent(self, intent_id: str) -> TradeIntent | None:
        from .models import IntentAction

        with self.connection() as conn:
            row = conn.execute("SELECT * FROM live_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            return TradeIntent(
                intent_id=str(data["intent_id"]), signal_id=str(data["signal_id"]),
                position_id=str(data["position_id"]), strategy=str(data["strategy"]),
                symbol=str(data["symbol"]), action=IntentAction(str(data["action"])),
                decision_time=int(data["decision_time"]), signal_price=Decimal(str(data["signal_price"])),
                strategy_stop_price=Decimal(str(data["strategy_stop_price"])), reason=str(data["reason"]),
                evidence=tuple(json.loads(data["evidence_json"] or "[]")),
            )

    def save_income(self, rows: list[dict[str, Any]]) -> int:
        saved = 0
        with self.connection() as conn:
            for row in rows:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO live_income VALUES(?,?,?,?,?,?,?)",
                    (
                        int(row.get("tranId") or row.get("id") or 0), str(row.get("incomeType") or ""),
                        str(row.get("asset") or ""), str(row.get("symbol") or ""),
                        str(row.get("income") or "0"), int(row.get("time") or 0),
                        json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                saved += max(0, cur.rowcount)
        return saved

    def trading_income_since(self, start_time: int) -> Decimal:
        included = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR")
        placeholders = ",".join("?" for _ in included)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT amount FROM live_income WHERE income_time>=? AND income_type IN ({placeholders})",
                (int(start_time), *included),
            ).fetchall()
            return sum((Decimal(str(row[0])) for row in rows), Decimal("0"))

    def exit_fill_summary(self, symbol: str, start_time: int) -> dict[str, Decimal]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT quantity,price,realized_pnl FROM live_fills
                WHERE symbol=? AND side='BUY' AND trade_time>=? ORDER BY trade_time""",
                (symbol, int(start_time)),
            ).fetchall()
        quantity = sum((Decimal(str(row[0])) for row in rows), Decimal("0"))
        notional = sum((Decimal(str(row[0])) * Decimal(str(row[1])) for row in rows), Decimal("0"))
        pnl = sum((Decimal(str(row[2])) for row in rows), Decimal("0"))
        return {
            "quantity": quantity,
            "average_price": notional / quantity if quantity > 0 else Decimal("0"),
            "realized_pnl": pnl,
        }

    def set_meta(self, key: str, value: str, updated_time: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO live_meta VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_time=excluded.updated_time",
                (key, value, updated_time),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM live_meta WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def consume_nonce(self, expected_hash: str, now_seconds: int, updated_time: int) -> bool:
        """Atomically consume the current real-order authorization nonce."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    "SELECT key,value FROM live_meta WHERE key IN (?,?,?)",
                    (
                        "real_order_nonce_hash",
                        "real_order_nonce_expires",
                        "real_order_nonce_used",
                    ),
                )
            }
            if (
                rows.get("real_order_nonce_hash") != expected_hash
                or rows.get("real_order_nonce_used", "1") == "1"
                or int(rows.get("real_order_nonce_expires", "0") or 0) < int(now_seconds)
            ):
                return False
            conn.execute(
                "UPDATE live_meta SET value='1',updated_time=? WHERE key='real_order_nonce_used'",
                (int(updated_time),),
            )
            return True

    def dashboard_snapshot(self, limit: int = 30) -> dict[str, Any]:
        with self.connection() as conn:
            account_row = conn.execute(
                "SELECT * FROM live_account_snapshots ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
            positions = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM live_positions ORDER BY updated_time DESC LIMIT ?", (int(limit),)
                )
            ]
            orders = [
                dict(row) for row in conn.execute(
                    """SELECT o.*,i.decision_time,i.signal_price,
                    CASE WHEN o.submit_time>0 THEN o.submit_time-i.decision_time ELSE NULL END AS signal_to_submit_ms,
                    CASE WHEN o.ack_time>0 AND o.submit_time>0 THEN o.ack_time-o.submit_time ELSE NULL END AS submit_to_ack_ms,
                    CASE WHEN o.first_fill_time>0 AND o.submit_time>0 THEN o.first_fill_time-o.submit_time ELSE NULL END AS submit_to_first_fill_ms,
                    CASE WHEN o.first_fill_time>0 THEN o.first_fill_time-i.decision_time ELSE NULL END AS signal_to_fill_ms,
                    CASE WHEN o.final_fill_time>0 THEN o.final_fill_time-i.decision_time ELSE NULL END AS signal_to_final_fill_ms
                    FROM live_orders o LEFT JOIN live_intents i ON i.intent_id=o.intent_id
                    ORDER BY o.updated_time DESC LIMIT ?""",
                    (int(limit),),
                )
            ]
            fills = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM live_fills ORDER BY trade_time DESC LIMIT ?", (int(limit),)
                )
            ]
            events = [
                dict(row) for row in conn.execute(
                    "SELECT event_time,event_type,correlation_id FROM live_events ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                )
            ]
        for row in positions:
            row["metadata"] = json.loads(row.pop("metadata_json", "{}") or "{}")
        account = dict(account_row) if account_row else None
        if account:
            account.pop("raw_json", None)
        return {
            "account": account,
            "positions": positions,
            "orders": orders,
            "fills": fills,
            "events": events,
            "safe_halt_reason": self.get_meta("safe_halt_reason"),
            "sizing": {
                "start_time": int(self.get_meta("sizing_start_time", "0") or 0),
                "initial_equity": self.get_meta("sizing_initial_equity", "0"),
                "current_equity": self.get_meta("sizing_current_equity", "0"),
                "peak_equity": self.get_meta("sizing_peak_equity", "0"),
                "drawdown_pct": self.get_meta("sizing_current_drawdown", "0"),
                "factor": self.get_meta("sizing_factor", "1"),
            },
            "service": {
                "heartbeat_time": int(self.get_meta("service_heartbeat_time", "0") or 0),
                "status": self.get_meta("service_status"),
                "pid": int(self.get_meta("service_pid", "0") or 0),
                "processed_events": int(self.get_meta("service_processed_events", "0") or 0),
                "processed_signals": int(
                    self.get_meta("service_processed_signals", "0") or 0
                ),
                "signal_source": self.get_meta("shared_signal_source_db"),
                "signal_strategy": self.get_meta("shared_signal_strategy"),
            },
        }
