from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .data.store import Store
from .timeutils import iso_from_ms, utc_ms


def run_web(settings: dict[str, Any], host: str = "127.0.0.1", port: int = 8787) -> None:
    store = Store(settings["paths"]["db_path"])

    class Handler(DashboardHandler):
        pass

    Handler.store = store
    Handler.settings = settings
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"dashboard API listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    store: Store
    settings: dict[str, Any] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self.write_json({"ok": True})
            elif parsed.path == "/api/summary":
                self.write_json(self.api_summary())
            elif parsed.path == "/api/liquidity":
                self.write_json({"rows": self.store.latest_liquidity(limit=int_param(query, "limit", 100))})
            elif parsed.path == "/api/pumps":
                self.write_json({"rows": decode_pumps(self.store.active_pump_rows(utc_ms(), limit=int_param(query, "limit", 100)))})
            elif parsed.path == "/api/pump-history":
                self.write_json({"rows": decode_pumps(self.store.pump_event_rows(limit=int_param(query, "limit", 300)))})
            elif parsed.path == "/api/long":
                self.write_json({"rows": self.store.active_long_rows(utc_ms(), limit=int_param(query, "limit", 100))})
            elif parsed.path == "/api/long-history":
                self.write_json({"rows": decode_longs(self.store.long_event_rows(limit=int_param(query, "limit", 300)))})
            elif parsed.path == "/api/alerts":
                self.write_json({"rows": decode_alerts(self.store.recent_alerts(limit=int_param(query, "limit", 100)))})
            elif parsed.path == "/api/backtests":
                self.write_json({"rows": decode_backtests(self.store.backtest_runs(limit=int_param(query, "limit", 20)))})
            elif parsed.path == "/api/model":
                self.write_json(read_model_meta())
            elif parsed.path == "/api/candles":
                symbol = (query.get("symbol", [""])[0] or "").upper()
                interval = query.get("interval", ["15m"])[0]
                limit = int_param(query, "limit", 160)
                start_raw = query.get("start_time", [""])[0]
                start_time = int(start_raw) if start_raw.isdigit() else None
                candles = self.store.load_candles(symbol, interval, start_time=start_time) if symbol else []
                self.write_json({"symbol": symbol, "interval": interval, "rows": [c.to_dict() for c in candles[-limit:]]})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.write_json({"error": f"{type(exc).__name__}: {exc}"})

    def api_summary(self) -> dict[str, Any]:
        data = self.store.dashboard_summary(utc_ms())
        for key in ("latest_snapshot_time", "latest_data_cutoff_time", "latest_alert_time"):
            value = data.get(key)
            data[f"{key}_iso"] = iso_from_ms(int(value)) if value else None
        signals = self.settings.get("signals", {})
        data["strategy"] = {
            "mode": signals.get("mode", ""),
            "strategy_version": signals.get("strategy_version", ""),
            "early_interval": signals.get("early_interval", ""),
            "confirm_interval": signals.get("confirm_interval", ""),
            "long_interval": signals.get("long_interval", ""),
            "multi_signal_cooldown_hours": signals.get("multi_signal_cooldown_hours", 4.0),
            "long_signal_cooldown_hours": signals.get("long_signal_cooldown_hours", 2.0),
            "lifecycle_long_watch_min_gain_pct": signals.get("lifecycle_long_watch_min_gain_pct", 15.0),
            "lifecycle_min_remaining_pct": signals.get("lifecycle_min_remaining_pct", 5.0),
            "long_enabled": bool(signals.get("long_enabled", False)),
        }
        return data

    def write_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def read_model_meta() -> dict[str, Any]:
    from pathlib import Path
    p = Path(__file__).resolve().parent / "ml" / "models" / "meta.json"
    lifecycle_p = Path(__file__).resolve().parent / "ml" / "models" / "lifecycle" / "meta.json"
    if not p.exists():
        return {"ready": False}
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        m.pop("feature_cols", None)  # 前端不需要, 省流量
        m.pop("long_feature_cols", None)
        if lifecycle_p.exists():
            lifecycle = json.loads(lifecycle_p.read_text(encoding="utf-8"))
            lifecycle.pop("feature_sets", None)
            m["lifecycle"] = lifecycle
            m["lifecycle_ready"] = True
        m["ready"] = True
        return m
    except Exception as exc:
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}


def int_param(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [default])[0])
    except Exception:
        return default


def decode_json_field(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else fallback
    except Exception:
        return fallback


def decode_alerts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["evidence"] = decode_json_field(item.pop("evidence_json", "[]"), [])
        item["risks"] = decode_json_field(item.pop("risks_json", "[]"), [])
        out.append(item)
    return out


def decode_pumps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["evidence"] = decode_json_field(item.pop("evidence_json", "[]"), [])
        out.append(item)
    return out


def decode_longs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["evidence"] = decode_json_field(item.pop("evidence_json", "[]"), [])
        out.append(item)
    return out


def decode_backtests(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        item["params"] = decode_json_field(item.pop("params_json", "{}"), {})
        item["metrics"] = decode_json_field(item.pop("metrics_json", "{}"), {})
        out.append(item)
    return out
