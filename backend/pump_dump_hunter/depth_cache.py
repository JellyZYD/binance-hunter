from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def book_imbalance(bid_notional: float, ask_notional: float) -> float:
    total = float(bid_notional) + float(ask_notional)
    if not math.isfinite(total) or total <= 0:
        return 0.0
    return (float(bid_notional) - float(ask_notional)) / total


class DepthSnapshotPublisher:
    """Publish an atomic, low-latency view of rolling top-20 depth snapshots."""

    def __init__(self, path: str | Path, baseline_ms: int = 120_000, retention_ms: int = 360_000):
        self.path = Path(path)
        self.baseline_ms = int(baseline_ms)
        self.retention_ms = max(int(retention_ms), self.baseline_ms * 2)
        self.history: dict[str, deque[dict[str, float]]] = defaultdict(deque)
        self.latest: dict[str, dict[str, float]] = {}

    def add(self, row: dict[str, Any]) -> dict[str, float] | None:
        symbol = str(row.get("symbol") or "").upper()
        ts = int(row.get("ts") or 0)
        bid = float(row.get("bid_notional20") or 0.0)
        ask = float(row.get("ask_notional20") or 0.0)
        if not symbol or ts <= 0 or bid <= 0 or ask <= 0:
            return None

        point = {
            "timestamp": float(ts),
            "bid_notional20": bid,
            "ask_notional20": ask,
            "imbalance20": book_imbalance(bid, ask),
        }
        history = self.history[symbol]
        history.append(point)
        cutoff = ts - self.retention_ms
        while history and int(history[0]["timestamp"]) < cutoff:
            history.popleft()

        target = ts - self.baseline_ms
        baseline = None
        for candidate in reversed(history):
            if int(candidate["timestamp"]) <= target:
                baseline = candidate
                break

        latest = dict(point)
        if baseline is not None:
            latest["baseline_timestamp"] = baseline["timestamp"]
            latest["baseline_imbalance20"] = baseline["imbalance20"]
            latest["imbalance_delta_2m"] = point["imbalance20"] - baseline["imbalance20"]
        self.latest[symbol] = latest
        return latest

    def flush(self, now_ms: int | None = None) -> None:
        if not self.latest:
            return
        updated = int(now_ms or max(row["timestamp"] for row in self.latest.values()))
        cutoff = updated - self.retention_ms
        symbols = {
            symbol: row
            for symbol, row in self.latest.items()
            if int(row.get("timestamp") or 0) >= cutoff
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_time": updated,
            "symbols": symbols,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)


class DepthSignalCache:
    """Read the collector cache and produce a fail-open paper signal gate."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_age_ms: int = 75_000,
        baseline_min_age_ms: int = 90_000,
        baseline_max_age_ms: int = 210_000,
        min_imbalance_delta: float = 0.0,
    ):
        self.path = Path(path)
        self.max_age_ms = int(max_age_ms)
        self.baseline_min_age_ms = int(baseline_min_age_ms)
        self.baseline_max_age_ms = int(baseline_max_age_ms)
        self.min_imbalance_delta = float(min_imbalance_delta)
        self._mtime_ns = -1
        self._symbols: dict[str, dict[str, Any]] = {}

    def _reload(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self._symbols = {}
            self._mtime_ns = -1
            return
        if mtime_ns == self._mtime_ns:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
                self._symbols = {}
            else:
                symbols = payload.get("symbols") or {}
                self._symbols = symbols if isinstance(symbols, dict) else {}
            self._mtime_ns = mtime_ns
        except (OSError, ValueError, TypeError):
            self._symbols = {}
            self._mtime_ns = mtime_ns

    def decision(self, symbol: str, now_ms: int) -> dict[str, Any]:
        self._reload()
        row = self._symbols.get(symbol.upper())
        if not isinstance(row, dict):
            return {"available": False, "ok": False, "reason": "bookdepth_missing"}

        ts = int(float(row.get("timestamp") or 0))
        baseline_ts = int(float(row.get("baseline_timestamp") or 0))
        age_ms = max(0, int(now_ms) - ts)
        baseline_span_ms = ts - baseline_ts
        if ts <= 0 or age_ms > self.max_age_ms:
            return {
                "available": False,
                "ok": False,
                "reason": "bookdepth_stale",
                "age_seconds": age_ms / 1000.0,
            }
        if baseline_ts <= 0 or not (self.baseline_min_age_ms <= baseline_span_ms <= self.baseline_max_age_ms):
            return {
                "available": False,
                "ok": False,
                "reason": "bookdepth_baseline_unready",
                "age_seconds": age_ms / 1000.0,
            }

        imbalance = float(row.get("imbalance20") or 0.0)
        baseline = float(row.get("baseline_imbalance20") or 0.0)
        delta = float(row.get("imbalance_delta_2m") or (imbalance - baseline))
        ok = delta >= self.min_imbalance_delta
        return {
            "available": True,
            "ok": ok,
            "reason": "bookdepth_imbalance_confirmed" if ok else "bookdepth_imbalance_rejected",
            "age_seconds": age_ms / 1000.0,
            "baseline_age_seconds": baseline_span_ms / 1000.0,
            "imbalance20": imbalance,
            "baseline_imbalance20": baseline,
            "imbalance_delta_2m": delta,
        }
