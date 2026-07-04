from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..models import Alert, Candle, LiquidityRecord, LongEvent, PumpEvent


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS candles(
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_volume REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    taker_buy_base REAL NOT NULL,
                    taker_buy_quote REAL NOT NULL,
                    PRIMARY KEY(symbol, interval, open_time)
                );

                CREATE TABLE IF NOT EXISTS liquidity_snapshots(
                    run_id TEXT NOT NULL,
                    snapshot_time INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    last_price REAL NOT NULL,
                    quote_volume_15m REAL NOT NULL,
                    quote_volume_30m REAL NOT NULL,
                    pct_15m REAL NOT NULL,
                    pct_30m REAL NOT NULL,
                    amp_15m REAL NOT NULL,
                    amp_30m REAL NOT NULL,
                    volume_ratio_15m REAL NOT NULL,
                    volume_ratio_30m REAL NOT NULL,
                    gain_rank_15m INTEGER NOT NULL,
                    gain_rank_30m INTEGER NOT NULL,
                    selected INTEGER NOT NULL,
                    pump_qualified INTEGER NOT NULL,
                    data_cutoff_time INTEGER NOT NULL,
                    pct_4h REAL NOT NULL DEFAULT 0,
                    pct_12h REAL NOT NULL DEFAULT 0,
                    pct_1d REAL NOT NULL DEFAULT 0,
                    quote_volume_4h REAL NOT NULL DEFAULT 0,
                    quote_volume_12h REAL NOT NULL DEFAULT 0,
                    quote_volume_1d REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS pump_events(
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    trigger_window TEXT NOT NULL,
                    anchor_price REAL NOT NULL,
                    high_price REAL NOT NULL,
                    high_time INTEGER NOT NULL,
                    current_price REAL NOT NULL,
                    max_gain_pct REAL NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    early_alerted_after_high_time INTEGER,
                    short_alerted_after_high_time INTEGER
                );

                CREATE TABLE IF NOT EXISTS watchlist(
                    symbol TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    high_price REAL NOT NULL,
                    high_time INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_update_time INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS long_events(
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    high_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    long_signal_seq INTEGER NOT NULL DEFAULT 0,
                    long_last_signal_time INTEGER,
                    status TEXT NOT NULL,
                    exit_reason TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS alerts(
                    alert_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    level TEXT NOT NULL,
                    decision_time INTEGER NOT NULL,
                    source_candle_close_time INTEGER NOT NULL,
                    data_cutoff_time INTEGER NOT NULL,
                    price REAL NOT NULL,
                    invalidation_price REAL NOT NULL,
                    anchor_price REAL NOT NULL,
                    high_price REAL NOT NULL,
                    remaining_downside_pct REAL NOT NULL,
                    volume_ratio REAL NOT NULL,
                    evidence_json TEXT NOT NULL,
                    risks_json TEXT NOT NULL,
                    pushed INTEGER NOT NULL DEFAULT 0,
                    push_error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS backtest_runs(
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    params_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    train_start INTEGER,
                    train_end INTEGER,
                    validation_start INTEGER,
                    validation_end INTEGER
                );
                """
            )
            ensure_columns(
                conn,
                "liquidity_snapshots",
                {
                    "pct_4h": "REAL NOT NULL DEFAULT 0",
                    "pct_12h": "REAL NOT NULL DEFAULT 0",
                    "pct_1d": "REAL NOT NULL DEFAULT 0",
                    "quote_volume_4h": "REAL NOT NULL DEFAULT 0",
                    "quote_volume_12h": "REAL NOT NULL DEFAULT 0",
                    "quote_volume_1d": "REAL NOT NULL DEFAULT 0",
                },
            )
            ensure_columns(
                conn,
                "pump_events",
                {
                    "fallback_alerted_after_high_time": "INTEGER",
                    "early_last_alert_time": "INTEGER",
                    "short_last_alert_time": "INTEGER",
                    "fallback_last_alert_time": "INTEGER",
                    "early_alert_seq": "INTEGER NOT NULL DEFAULT 0",
                    "short_signal_seq": "INTEGER NOT NULL DEFAULT 0",
                    "fallback_alert_seq": "INTEGER NOT NULL DEFAULT 0",
                    "lifecycle_mode": "TEXT NOT NULL DEFAULT ''",
                    "behavior_state": "TEXT NOT NULL DEFAULT ''",
                    "lifecycle_updated_time": "INTEGER",
                    "route_mode": "TEXT NOT NULL DEFAULT 'unknown'",
                    "route_candidate": "TEXT NOT NULL DEFAULT ''",
                    "route_confidence": "REAL NOT NULL DEFAULT 0",
                    "route_margin": "REAL NOT NULL DEFAULT 0",
                    "route_streak": "INTEGER NOT NULL DEFAULT 0",
                    "route_probs_json": "TEXT NOT NULL DEFAULT '{}'",
                    "route_updated_time": "INTEGER",
                },
            )
            ensure_columns(
                conn,
                "long_events",
                {
                    "long_last_signal_time": "INTEGER",
                    "qv30_rank": "INTEGER NOT NULL DEFAULT 0",
                    "ret30_rank": "INTEGER NOT NULL DEFAULT 0",
                    "qv30_rank_pct": "REAL NOT NULL DEFAULT 0",
                    "ret30_rank_pct": "REAL NOT NULL DEFAULT 0",
                },
            )
            ensure_columns(
                conn,
                "alerts",
                {
                    "occurrence": "INTEGER NOT NULL DEFAULT 0",
                    "category": "TEXT NOT NULL DEFAULT ''",
                    "lifecycle_mode": "TEXT NOT NULL DEFAULT ''",
                    "behavior_state": "TEXT NOT NULL DEFAULT ''",
                    "model_name": "TEXT NOT NULL DEFAULT ''",
                    "model_score": "REAL NOT NULL DEFAULT 0",
                    "model_threshold": "REAL NOT NULL DEFAULT 0",
                    "signal_interval": "TEXT NOT NULL DEFAULT ''",
                    "route_mode": "TEXT NOT NULL DEFAULT ''",
                    "route_confidence": "REAL NOT NULL DEFAULT 0",
                    "route_margin": "REAL NOT NULL DEFAULT 0",
                },
            )
            conn.commit()
        finally:
            conn.close()

    def save_candles(self, candles: list[Candle]) -> int:
        if not candles:
            return 0
        conn = self.connect()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO candles(
                    symbol, interval, open_time, close_time, open, high, low, close,
                    volume, quote_volume, trades, taker_buy_base, taker_buy_quote
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        c.symbol,
                        c.interval,
                        c.open_time,
                        c.close_time,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                        c.volume,
                        c.quote_volume,
                        c.trades,
                        c.taker_buy_base,
                        c.taker_buy_quote,
                    )
                    for c in candles
                ],
            )
            conn.commit()
            return len(candles)
        finally:
            conn.close()

    def load_candles(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[Candle]:
        sql = "SELECT * FROM candles WHERE symbol=? AND interval=?"
        args: list[Any] = [symbol.upper(), interval]
        if start_time is not None:
            sql += " AND open_time>=?"
            args.append(start_time)
        if end_time is not None:
            sql += " AND close_time<=?"
            args.append(end_time)
        sql += " ORDER BY open_time"
        conn = self.connect()
        try:
            rows = conn.execute(sql, args).fetchall()
            return [row_to_candle(r) for r in rows]
        finally:
            conn.close()

    def candle_symbols(self, interval: str = "1m") -> list[str]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT DISTINCT symbol FROM candles WHERE interval=? ORDER BY symbol", (interval,)).fetchall()
            return [str(r["symbol"]) for r in rows]
        finally:
            conn.close()

    def max_candle_close_time(self, interval: str = "1m", symbols: list[str] | None = None) -> int:
        sql = "SELECT MAX(close_time) AS close_time FROM candles WHERE interval=?"
        args: list[Any] = [interval]
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            sql += f" AND symbol IN ({placeholders})"
            args.extend([s.upper() for s in symbols])
        conn = self.connect()
        try:
            row = conn.execute(sql, args).fetchone()
            return int(row["close_time"] or 0) if row else 0
        finally:
            conn.close()

    def save_liquidity_snapshot(self, run_id: str, snapshot_time: int, records: list[LiquidityRecord]) -> None:
        conn = self.connect()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO liquidity_snapshots(
                    run_id, snapshot_time, symbol, rank, last_price, quote_volume_15m, quote_volume_30m,
                    pct_15m, pct_30m, amp_15m, amp_30m, volume_ratio_15m, volume_ratio_30m,
                    gain_rank_15m, gain_rank_30m, selected, pump_qualified, data_cutoff_time,
                    pct_4h, pct_12h, pct_1d, quote_volume_4h, quote_volume_12h, quote_volume_1d
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id,
                        snapshot_time,
                        r.symbol,
                        r.rank,
                        r.last_price,
                        r.quote_volume_15m,
                        r.quote_volume_30m,
                        r.pct_15m,
                        r.pct_30m,
                        r.amp_15m,
                        r.amp_30m,
                        r.volume_ratio_15m,
                        r.volume_ratio_30m,
                        r.gain_rank_15m,
                        r.gain_rank_30m,
                        1 if r.selected else 0,
                        1 if r.pump_qualified else 0,
                        r.data_cutoff_time,
                        r.pct_4h,
                        r.pct_12h,
                        r.pct_1d,
                        r.quote_volume_4h,
                        r.quote_volume_12h,
                        r.quote_volume_1d,
                    )
                    for r in records
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def upsert_pump_events(self, events: list[PumpEvent]) -> None:
        if not events:
            return
        conn = self.connect()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO pump_events(
                    event_id, symbol, first_seen, last_seen, expires_at, trigger_window,
                    anchor_price, high_price, high_time, current_price, max_gain_pct,
                    status, evidence_json, early_alerted_after_high_time, short_alerted_after_high_time,
                    fallback_alerted_after_high_time, early_last_alert_time, short_last_alert_time,
                    fallback_last_alert_time, early_alert_seq, short_signal_seq, fallback_alert_seq,
                    lifecycle_mode, behavior_state, lifecycle_updated_time,
                    route_mode, route_candidate, route_confidence, route_margin, route_streak,
                    route_probs_json, route_updated_time
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        e.event_id,
                        e.symbol,
                        e.first_seen,
                        e.last_seen,
                        e.expires_at,
                        e.trigger_window,
                        e.anchor_price,
                        e.high_price,
                        e.high_time,
                        e.current_price,
                        e.max_gain_pct,
                        e.status,
                        json.dumps(e.evidence, ensure_ascii=False),
                        e.early_alerted_after_high_time,
                        e.short_alerted_after_high_time,
                        e.fallback_alerted_after_high_time,
                        e.early_last_alert_time,
                        e.short_last_alert_time,
                        e.fallback_last_alert_time,
                        e.early_alert_seq,
                        e.short_signal_seq,
                        e.fallback_alert_seq,
                        e.lifecycle_mode,
                        e.behavior_state,
                        e.lifecycle_updated_time,
                        e.route_mode,
                        e.route_candidate,
                        e.route_confidence,
                        e.route_margin,
                        e.route_streak,
                        json.dumps(e.route_probs, ensure_ascii=False),
                        e.route_updated_time,
                    )
                    for e in events
                ],
            )
            conn.executemany(
                """INSERT OR REPLACE INTO watchlist(
                    symbol, event_id, status, high_price, high_time, expires_at, last_update_time
                ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (e.symbol, e.event_id, e.status, e.high_price, e.high_time, e.expires_at, e.last_seen)
                    for e in events
                    if e.status == "active"
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def upsert_long_events(self, events: list[LongEvent]) -> None:
        if not events:
            return
        conn = self.connect()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO long_events(
                    event_id, symbol, first_seen, last_seen, expires_at, entry_price,
                    high_price, current_price, long_signal_seq, long_last_signal_time, status, exit_reason, evidence_json,
                    qv30_rank, ret30_rank, qv30_rank_pct, ret30_rank_pct
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        e.event_id, e.symbol, e.first_seen, e.last_seen, e.expires_at, e.entry_price,
                        e.high_price, e.current_price, e.long_signal_seq, e.long_last_signal_time, e.status, e.exit_reason,
                        json.dumps(e.evidence, ensure_ascii=False),
                        e.qv30_rank, e.ret30_rank, e.qv30_rank_pct, e.ret30_rank_pct,
                    )
                    for e in events
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def active_long_events(self, now_ms: int) -> list[LongEvent]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM long_events WHERE status='active' AND expires_at>=? ORDER BY symbol", (now_ms,)
            ).fetchall()
            dedup: dict[str, LongEvent] = {}
            for row in rows:
                e = row_to_long_event(row)
                dedup[e.symbol] = e
            return list(dedup.values())
        finally:
            conn.close()

    def active_long_rows(self, now_ms: int, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM long_events WHERE status='active' AND expires_at>=? ORDER BY last_seen DESC LIMIT ?",
                (now_ms, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def long_event_rows(self, limit: int = 300) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM long_events ORDER BY last_seen DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def active_pump_events(self, now_ms: int) -> list[PumpEvent]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM pump_events WHERE status='active' AND expires_at>=? ORDER BY symbol, last_seen, first_seen",
                (now_ms,),
            ).fetchall()
            dedup: dict[str, PumpEvent] = {}
            for row in rows:
                event = row_to_event(row)
                dedup[event.symbol] = event
            return sorted(dedup.values(), key=lambda e: (e.last_seen, e.max_gain_pct), reverse=True)
        finally:
            conn.close()

    def get_pump_event(self, event_id: str) -> PumpEvent | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM pump_events WHERE event_id=?", (event_id,)).fetchone()
            return row_to_event(row) if row else None
        finally:
            conn.close()

    def save_alert(self, alert: Alert, pushed: bool = False, push_error: str = "") -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO alerts(
                    alert_id, event_id, symbol, level, decision_time, source_candle_close_time,
                    data_cutoff_time, price, invalidation_price, anchor_price, high_price,
                    remaining_downside_pct, volume_ratio, evidence_json, risks_json, pushed, push_error,
                    occurrence, category, lifecycle_mode, behavior_state, model_name, model_score,
                    model_threshold, signal_interval, route_mode, route_confidence, route_margin
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    alert.alert_id,
                    alert.event_id,
                    alert.symbol,
                    alert.level,
                    alert.decision_time,
                    alert.source_candle_close_time,
                    alert.data_cutoff_time,
                    alert.price,
                    alert.invalidation_price,
                    alert.anchor_price,
                    alert.high_price,
                    alert.remaining_downside_pct,
                    alert.volume_ratio,
                    json.dumps(alert.evidence, ensure_ascii=False),
                    json.dumps(alert.risks, ensure_ascii=False),
                    1 if pushed else 0,
                    push_error,
                    alert.occurrence,
                    alert.category,
                    alert.lifecycle_mode,
                    alert.behavior_state,
                    alert.model_name,
                    alert.model_score,
                    alert.model_threshold,
                    alert.signal_interval,
                    alert.route_mode,
                    alert.route_confidence,
                    alert.route_margin,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM alerts ORDER BY decision_time DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def latest_liquidity(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            latest = conn.execute("SELECT run_id FROM liquidity_snapshots ORDER BY snapshot_time DESC LIMIT 1").fetchone()
            if not latest:
                return []
            rows = conn.execute(
                "SELECT * FROM liquidity_snapshots WHERE run_id=? ORDER BY rank LIMIT ?",
                (latest["run_id"], int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def active_pump_rows(self, now_ms: int, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT * FROM pump_events
                WHERE status='active' AND expires_at>=?
                ORDER BY symbol, last_seen, first_seen""",
                (now_ms,),
            ).fetchall()
            dedup: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = dict(row)
                dedup[str(item["symbol"])] = item
            ordered = sorted(dedup.values(), key=lambda r: (float(r["max_gain_pct"]), int(r["last_seen"])), reverse=True)
            return ordered[: int(limit)]
        finally:
            conn.close()

    def pump_event_rows(self, limit: int = 300) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT * FROM pump_events
                ORDER BY last_seen DESC, max_gain_pct DESC
                LIMIT ?""",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def backtest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def dashboard_summary(self, now_ms: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            tables = {}
            for name in ("candles", "liquidity_snapshots", "pump_events", "watchlist", "alerts", "backtest_runs"):
                tables[name] = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
            latest_snapshot = conn.execute(
                "SELECT MAX(snapshot_time) AS ts, MAX(data_cutoff_time) AS cutoff FROM liquidity_snapshots"
            ).fetchone()
            latest_alert = conn.execute("SELECT MAX(decision_time) AS ts FROM alerts").fetchone()
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM pump_events WHERE status='active' AND expires_at>=?",
                (now_ms,),
            ).fetchone()["n"]
            return {
                "tables": tables,
                "latest_snapshot_time": latest_snapshot["ts"],
                "latest_data_cutoff_time": latest_snapshot["cutoff"],
                "latest_alert_time": latest_alert["ts"],
                "active_pump_events": active,
            }
        finally:
            conn.close()
    def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def save_backtest_run(
        self,
        run_id: str,
        created_at: str,
        days: int,
        params: dict[str, Any],
        metrics: dict[str, Any],
        train_start: int | None,
        train_end: int | None,
        validation_start: int | None,
        validation_end: int | None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO backtest_runs(
                    run_id, created_at, days, params_json, metrics_json,
                    train_start, train_end, validation_start, validation_end
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    created_at,
                    days,
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False),
                    train_start,
                    train_end,
                    validation_start,
                    validation_end,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def row_to_candle(row: sqlite3.Row) -> Candle:
    return Candle(
        symbol=str(row["symbol"]),
        interval=str(row["interval"]),
        open_time=int(row["open_time"]),
        close_time=int(row["close_time"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        quote_volume=float(row["quote_volume"]),
        trades=int(row["trades"]),
        taker_buy_base=float(row["taker_buy_base"]),
        taker_buy_quote=float(row["taker_buy_quote"]),
    )


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def row_to_event(row: sqlite3.Row) -> PumpEvent:
    return PumpEvent(
        event_id=str(row["event_id"]),
        symbol=str(row["symbol"]),
        first_seen=int(row["first_seen"]),
        last_seen=int(row["last_seen"]),
        expires_at=int(row["expires_at"]),
        trigger_window=str(row["trigger_window"]),
        anchor_price=float(row["anchor_price"]),
        high_price=float(row["high_price"]),
        high_time=int(row["high_time"]),
        current_price=float(row["current_price"]),
        max_gain_pct=float(row["max_gain_pct"]),
        status=str(row["status"]),
        evidence=json.loads(row["evidence_json"]),
        early_alerted_after_high_time=row["early_alerted_after_high_time"],
        short_alerted_after_high_time=row["short_alerted_after_high_time"],
        fallback_alerted_after_high_time=row_get(row, "fallback_alerted_after_high_time"),
        early_last_alert_time=row_get(row, "early_last_alert_time"),
        short_last_alert_time=row_get(row, "short_last_alert_time"),
        fallback_last_alert_time=row_get(row, "fallback_last_alert_time"),
        early_alert_seq=row_get(row, "early_alert_seq") or 0,
        short_signal_seq=row_get(row, "short_signal_seq") or 0,
        fallback_alert_seq=row_get(row, "fallback_alert_seq") or 0,
        lifecycle_mode=str(row_get(row, "lifecycle_mode") or ""),
        behavior_state=str(row_get(row, "behavior_state") or ""),
        lifecycle_updated_time=row_get(row, "lifecycle_updated_time"),
        route_mode=str(row_get(row, "route_mode") or "unknown"),
        route_candidate=str(row_get(row, "route_candidate") or ""),
        route_confidence=float(row_get(row, "route_confidence") or 0.0),
        route_margin=float(row_get(row, "route_margin") or 0.0),
        route_streak=int(row_get(row, "route_streak") or 0),
        route_probs=json.loads(row_get(row, "route_probs_json") or "{}"),
        route_updated_time=row_get(row, "route_updated_time"),
    )


def row_to_long_event(row: sqlite3.Row) -> LongEvent:
    return LongEvent(
        event_id=str(row["event_id"]),
        symbol=str(row["symbol"]),
        first_seen=int(row["first_seen"]),
        last_seen=int(row["last_seen"]),
        expires_at=int(row["expires_at"]),
        entry_price=float(row["entry_price"]),
        high_price=float(row["high_price"]),
        current_price=float(row["current_price"]),
        long_signal_seq=int(row_get(row, "long_signal_seq") or 0),
        long_last_signal_time=row_get(row, "long_last_signal_time"),
        status=str(row["status"]),
        exit_reason=str(row_get(row, "exit_reason") or ""),
        evidence=json.loads(row_get(row, "evidence_json") or "[]"),
        qv30_rank=int(row_get(row, "qv30_rank") or 0),
        ret30_rank=int(row_get(row, "ret30_rank") or 0),
        qv30_rank_pct=float(row_get(row, "qv30_rank_pct") or 0.0),
        ret30_rank_pct=float(row_get(row, "ret30_rank_pct") or 0.0),
    )


def row_get(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None
