"""Lightweight server health metrics (pure stdlib, no psutil).

Reads Linux /proc for CPU/memory/network; shutil for disk. All /proc access is
guarded so it degrades to nulls on non-Linux (e.g. Windows dev box). CPU% and
network throughput are computed as deltas between consecutive calls, cached at
module level so the stateless HTTP handler still gets real rates.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

_prev: dict[str, Any] = {"t": 0.0, "cpu": None, "net": None}


def read_self_rss_mb() -> float:
    """Resident memory of the CURRENT process (the monitor), pure /proc."""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB -> MB
    except Exception:
        pass
    return 0.0


def write_monitor_health(root: Path, stats: dict[str, Any]) -> None:
    """Monitor process writes its health so the (separate) API process can show
    it on the dashboard: RSS, events processed, open positions, universe size."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        payload = {**stats, "rss_mb": read_self_rss_mb(), "ts": int(time.time() * 1000)}
        p = root / "monitor_health.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(p)
    except Exception:
        pass


def read_monitor_health(root: Path) -> dict[str, Any] | None:
    try:
        p = root / "monitor_health.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        age = time.time() - int(data.get("ts", 0)) / 1000
        data["age_sec"] = int(age)
        data["alive"] = age < 120  # heartbeat within 2min = monitor healthy
        return data
    except Exception:
        return None


def _read_cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r") as fh:
            parts = fh.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle
    except Exception:
        return None


def _read_net_bytes() -> tuple[int, int] | None:
    try:
        rx = tx = 0
        with open("/proc/net/dev", "r") as fh:
            for line in fh.readlines()[2:]:
                iface, _, rest = line.partition(":")
                if iface.strip() in ("lo",):
                    continue
                cols = rest.split()
                if len(cols) >= 9:
                    rx += int(cols[0])
                    tx += int(cols[8])
        return rx, tx
    except Exception:
        return None


def _read_mem() -> dict[str, Any]:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - avail)
        return {
            "total_mb": round(total / 1024, 1),
            "used_mb": round(used / 1024, 1),
            "percent": round(used / total * 100, 1) if total else 0.0,
            "swap_used_mb": round(max(0, info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1024, 1),
        }
    except Exception:
        return {"total_mb": 0.0, "used_mb": 0.0, "percent": 0.0, "swap_used_mb": 0.0}


def _dir_stats(path: Path) -> dict[str, Any]:
    total = 0
    oldest = None
    newest = None
    count = 0
    if path.is_dir():
        for p in path.rglob("*"):
            try:
                if not p.is_file():
                    continue
                st = p.stat()
                total += st.st_size
                count += 1
                oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
                newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
            except OSError:
                continue
    span_h = round((newest - oldest) / 3600, 1) if (oldest and newest) else 0.0
    return {"mb": round(total / 1_048_576, 1), "files": count, "span_hours": span_h}


def collect(db_path: Path, candle_stat: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    dt = now - _prev["t"] if _prev["t"] else 0.0

    # CPU %
    cpu_pct = 0.0
    cur_cpu = _read_cpu_times()
    if cur_cpu and _prev["cpu"] and dt > 0:
        d_tot = cur_cpu[0] - _prev["cpu"][0]
        d_idle = cur_cpu[1] - _prev["cpu"][1]
        if d_tot > 0:
            cpu_pct = round(max(0.0, min(100.0, (1 - d_idle / d_tot) * 100)), 1)
    _prev["cpu"] = cur_cpu

    # Network throughput
    rx_kbps = tx_kbps = 0.0
    rx_total = tx_total = 0
    cur_net = _read_net_bytes()
    if cur_net:
        rx_total, tx_total = cur_net
        if _prev["net"] and dt > 0:
            rx_kbps = round(max(0, cur_net[0] - _prev["net"][0]) / dt / 1024, 1)
            tx_kbps = round(max(0, cur_net[1] - _prev["net"][1]) / dt / 1024, 1)
    _prev["net"] = cur_net
    _prev["t"] = now

    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = 0.0

    storage_root = db_path.parent
    du = shutil.disk_usage(storage_root)
    micro_dir = storage_root / "micro"

    db_mb = round(db_path.stat().st_size / 1_048_576, 1) if db_path.exists() else 0.0

    binance = {"connected": False, "last_candle_age_sec": None, "candles_1m": 0}
    if candle_stat:
        last_close = candle_stat.get("last_close_ms") or 0
        binance["candles_1m"] = int(candle_stat.get("count") or 0)
        if last_close:
            age = max(0, int((now * 1000 - last_close) / 1000))
            binance["last_candle_age_sec"] = age
            binance["connected"] = age <= 180  # 1m candle within 3min = stream healthy
        binance["candle_span_days"] = candle_stat.get("span_days")

    return {
        "ts": int(now * 1000),
        "cpu": {"percent": cpu_pct, "cores": os.cpu_count() or 0,
                "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2)},
        "memory": _read_mem(),
        "disk": {"total_gb": round(du.total / 1_073_741_824, 1),
                 "used_gb": round(du.used / 1_073_741_824, 1),
                 "free_gb": round(du.free / 1_073_741_824, 1),
                 "percent": round(du.used / du.total * 100, 1) if du.total else 0.0},
        "network": {"rx_kbps": rx_kbps, "tx_kbps": tx_kbps,
                    "rx_total_gb": round(rx_total / 1_073_741_824, 2),
                    "tx_total_gb": round(tx_total / 1_073_741_824, 2)},
        "binance": binance,
        "data": {"db_mb": db_mb, "micro": _dir_stats(micro_dir)},
        "monitor": read_monitor_health(storage_root),
    }
