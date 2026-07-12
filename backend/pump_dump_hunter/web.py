from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .data.store import Store
from .timeutils import iso_from_ms, utc_ms
from .waterfall import waterfall_settings


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
                limit = int_param(query, "limit", 100)
                include_shadow = bool_param(query, "include_shadow", False)
                min_gain = formal_pump_min_gain(self.settings)
                long_min_gain = long_pump_min_gain(self.settings)
                rows = annotate_pumps(
                    decode_pumps(self.store.active_pump_rows(utc_ms(), limit=max(300, limit * 4))),
                    min_gain,
                    long_min_gain,
                )
                shadow_count = sum(1 for row in rows if not row.get("is_formal_watch"))
                if not include_shadow:
                    rows = [row for row in rows if row.get("is_formal_watch")]
                self.write_json({"rows": rows[:limit], "shadow_count": shadow_count, "formal_min_gain_pct": min_gain})
            elif parsed.path == "/api/pump-history":
                self.write_json({
                    "rows": annotate_pumps(
                        decode_pumps(self.store.pump_event_rows(limit=int_param(query, "limit", 300))),
                        formal_pump_min_gain(self.settings),
                        long_pump_min_gain(self.settings),
                    )
                })
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
            elif parsed.path == "/api/waterfall/summary":
                self.write_json(self.api_waterfall_summary())
            elif parsed.path == "/api/waterfall/watch":
                self.write_json({"rows": self.store.waterfall_watch_rows(limit=int_param(query, "limit", 300))})
            elif parsed.path == "/api/waterfall/positions":
                rows = self.store.waterfall_position_rows(
                    status=query.get("status", [""])[0],
                    limit=int_param(query, "limit", 200),
                    strategy=query.get("strategy", [""])[0],
                )
                self.write_json({"rows": enrich_waterfall_positions(self.store, rows)})
            elif parsed.path == "/api/waterfall/signals":
                self.write_json({"rows": self.store.waterfall_signal_rows(
                    limit=int_param(query, "limit", 200),
                    strategy=query.get("strategy", [""])[0],
                )})
            elif parsed.path == "/api/waterfall/shadow":
                self.write_json({
                    "summary": self.store.waterfall_shadow_summary(),
                    "rows": self.store.waterfall_shadow_rows(limit=int_param(query, "limit", 200)),
                })
            elif parsed.path == "/api/waterfall/replay-results":
                self.write_json(read_waterfall_replay_results(limit=int_param(query, "limit", 20)))
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
        min_gain = formal_pump_min_gain(self.settings)
        active_pumps = annotate_pumps(
            decode_pumps(self.store.active_pump_rows(utc_ms(), limit=2000)),
            min_gain,
            long_pump_min_gain(self.settings),
        )
        formal_count = sum(1 for row in active_pumps if row.get("is_formal_watch"))
        data["raw_active_pump_events"] = data.get("active_pump_events", 0)
        data["active_pump_events"] = formal_count
        data["shadow_pump_events"] = max(0, len(active_pumps) - formal_count)
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
            "long_emit_once_per_event": bool(signals.get("long_emit_once_per_event", True)),
            "long_max_signal_gain_pct": signals.get("long_max_signal_gain_pct", 10.0),
            "long_max_signal_delay_hours": signals.get("long_max_signal_delay_hours", 2.0),
            "lifecycle_long_watch_min_gain_pct": signals.get("lifecycle_long_watch_min_gain_pct", 15.0),
            "lifecycle_min_remaining_pct": signals.get("lifecycle_min_remaining_pct", 5.0),
            "lifecycle_route_confirm_bars": signals.get("lifecycle_route_confirm_bars", 2),
            "lifecycle_route_margin": signals.get("lifecycle_route_margin", 0.12),
            "lifecycle_dynamic_route_thresholds": bool(signals.get("lifecycle_dynamic_route_thresholds", True)),
            "lifecycle_route_fast_threshold": signals.get("lifecycle_route_fast_threshold", 0.914496),
            "lifecycle_route_slow_threshold": signals.get("lifecycle_route_slow_threshold", 0.701967),
            "lifecycle_route_fast_break_threshold": signals.get("lifecycle_route_fast_break_threshold", 0.914496),
            "lifecycle_route_slow_break_threshold": signals.get("lifecycle_route_slow_break_threshold", 0.701967),
            "lifecycle_pump_signal_min_gain_pct": signals.get("lifecycle_pump_signal_min_gain_pct", 0.0),
            "lifecycle_formal_watch_min_gain_pct": min_gain,
            "lifecycle_high_pump_enabled": bool(signals.get("lifecycle_high_pump_enabled", False)),
            "lifecycle_high_pump_min_gain_pct": signals.get("lifecycle_high_pump_min_gain_pct", 40.0),
            "long_enabled": bool(signals.get("long_enabled", False)),
        }
        return data

    def api_waterfall_summary(self) -> dict[str, Any]:
        from .board_waterfall import STRATEGY_NAME as CLAUDE_STRATEGY
        from .board_waterfall import board_waterfall_settings
        from .waterfall import strategy_label

        cfg = waterfall_settings(self.settings)
        out = self.store.waterfall_summary(float(cfg.get("paper_initial_balance_usdt") or 0.0))
        board_cfg = board_waterfall_settings(self.settings)
        accounts = []
        known = [f"waterfall_{cfg.get('variant', 'core5_agg')}_1m"]
        if bool(board_cfg.get("enabled", True)):
            known.append(CLAUDE_STRATEGY)
        strategies = list(dict.fromkeys([*known, *(self.store.waterfall_strategies() or [])]))
        for strat in strategies:
            init = (
                float(board_cfg.get("paper_initial_balance_usdt") or 0.0)
                if strat == CLAUDE_STRATEGY
                else float(cfg.get("paper_initial_balance_usdt") or 0.0)
            )
            acc = self.store.waterfall_summary(init, strategy=strat)
            acc["strategy"] = strat
            acc["strategy_label"] = strategy_label(strat)
            accounts.append(acc)
        out["accounts"] = accounts
        runtime = self.settings.get("runtime", {})
        out["active_strategy"] = runtime.get("active_strategy", "waterfall_quant")
        out["config"] = {
            "variant": cfg.get("variant"),
            "broad_top": cfg.get("broad_top"),
            "watch_interval": cfg.get("watch_interval"),
            "discover_every": cfg.get("discover_every"),
            "prewarm_limit": cfg.get("prewarm_limit"),
            "same_symbol_cooldown_hours": cfg.get("same_symbol_cooldown_hours"),
            "after_stop_cooldown_hours": cfg.get("after_stop_cooldown_hours"),
            "family_gap_minutes": cfg.get("family_gap_minutes"),
            "max_trades_per_symbol_day": cfg.get("max_trades_per_symbol_day"),
            "notional_usdt": cfg.get("notional_usdt"),
            "paper_initial_balance_usdt": cfg.get("paper_initial_balance_usdt"),
            "paper_margin_fraction": cfg.get("paper_margin_fraction"),
            "leverage": cfg.get("leverage"),
            "max_open_positions": cfg.get("max_open_positions"),
            "enabled_families": cfg.get("enabled_families", []),
            "micro_streams": cfg.get("micro_streams", []),
            "require_agg_confirmation": bool(cfg.get("require_agg_confirmation", True)),
            "agg_sell_ratio_min": cfg.get("agg_sell_ratio_min"),
            "agg_low_time_frac_min": cfg.get("agg_low_time_frac_min"),
            "strong_agg_sell_ratio_min": cfg.get("strong_agg_sell_ratio_min"),
            "strong_agg_close_pos_max": cfg.get("strong_agg_close_pos_max"),
            "execution_mode": cfg.get("execution_mode", "paper"),
            "real_order_enabled": bool(cfg.get("real_order_enabled", False)),
            "push_wecom": bool(cfg.get("push_wecom", True)),
        }
        return out

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


def read_waterfall_replay_results(limit: int = 20) -> dict[str, Any]:
    rows = []
    storage = Path(__file__).resolve().parents[1] / "storage" / "ml"

    agg_root = storage / "agg_waterfall_replay"
    for path in sorted(agg_root.glob("agg_waterfall_metrics_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            item["path"] = str(path)
            item["updated_time"] = int(path.stat().st_mtime * 1000)
            item["result_type"] = "agg_direct"
            item["mode"] = item.get("mode", "agg_direct")
            rows.append(item)
        except Exception:
            continue

    compare_root = storage / "waterfall_mode_compare"
    for path in sorted(compare_root.glob("waterfall_mode_compare_metrics_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            common = {
                "result_type": "mode_compare",
                "path": str(path),
                "updated_time": int(path.stat().st_mtime * 1000),
                "start": item.get("start"),
                "end": item.get("end"),
                "symbols": item.get("symbols"),
                "variant": item.get("variant"),
                "families": item.get("families", []),
            }
            for mode in ("kline", "agg"):
                metrics = item.get(mode) or {}
                rows.append({**common, "mode": mode, **metrics})
        except Exception:
            continue
    rows.sort(key=lambda r: int(r.get("updated_time") or 0), reverse=True)
    return {"rows": rows}


def enrich_waterfall_positions(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    watch = {str(r.get("symbol") or ""): r for r in store.waterfall_watch_rows(limit=2000)}
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        symbol = str(item.get("symbol") or "")
        entry = float(item.get("entry_price") or 0.0)
        status = str(item.get("status") or "")
        mark = float((watch.get(symbol) or {}).get("last_price") or item.get("exit_price") or entry or 0.0)
        notional = float(item.get("notional_usdt") or 0.0)
        fee_rate = float(item.get("fee_rate") or 0.0)
        margin = float(item.get("margin_usdt") or 0.0)
        if status == "open" and entry > 0 and mark > 0:
            pnl_pct = 1.0 - mark / entry - fee_rate
            pnl_usdt = notional * pnl_pct
        else:
            pnl_pct = float(item.get("pnl_pct") or 0.0)
            pnl_usdt = float(item.get("pnl_usdt") or 0.0)
        item["mark_price"] = mark
        item["unrealized_pnl_pct"] = pnl_pct if status == "open" else 0.0
        item["unrealized_pnl_usdt"] = pnl_usdt if status == "open" else 0.0
        item["margin_roi_pct"] = (pnl_usdt / margin) if margin > 0 else 0.0
        out.append(item)
    return out


def int_param(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [default])[0])
    except Exception:
        return default


def bool_param(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = str(query.get(key, [str(int(default))])[0]).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def formal_pump_min_gain(settings: dict[str, Any]) -> float:
    try:
        return float((settings.get("signals") or {}).get("lifecycle_pump_signal_min_gain_pct", 0.0) or 0.0)
    except Exception:
        return 0.0


def long_pump_min_gain(settings: dict[str, Any]) -> float:
    try:
        return float((settings.get("signals") or {}).get("lifecycle_long_watch_min_gain_pct", 15.0) or 15.0)
    except Exception:
        return 15.0


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
        item["route_probs"] = decode_json_field(item.pop("route_probs_json", "{}"), {})
        out.append(item)
    return out


def annotate_pumps(rows: list[dict[str, Any]], formal_min_gain_pct: float, long_min_gain_pct: float) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        try:
            max_gain = float(item.get("max_gain_pct", 0.0) or 0.0)
        except Exception:
            max_gain = 0.0
        evidence = item.get("evidence") or []
        long_derived = any(str(e).startswith("source=long_signal_pump_watch") for e in evidence)
        required_gain = long_min_gain_pct if long_derived else formal_min_gain_pct
        formal = required_gain <= 0 or max_gain >= required_gain
        item["is_formal_watch"] = formal
        item["formal_watch_min_gain_pct"] = required_gain
        item["monitor_stage"] = "formal" if formal else "shadow"
        item["monitor_stage_label"] = "formal_watch" if formal else "shadow_watch"
        item["long_derived_watch"] = long_derived
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
