from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

from .backtest import optimize_from_store, run_backtest_from_store
from .config import ensure_dirs, load_settings
from .data.bb_importer import import_bb_klines
from .data.rest_client import BinanceRestClient
from .data.store import Store
from .discovery import build_broad_universe, discover_recent_liquidity
from .engine.signal_engine import SignalEngine
from .live import emit_alerts, monitor
from .live_trading.config import LiveTradingConfig
from .live_trading.credentials import CredentialError
from .live_trading.gateway import GatewayError
from .live_trading.service import (
    ClaudeLiveTradingService,
    build_runtime,
    consume_order_nonce,
    issue_order_nonce,
    live_preflight,
)
from .live_trading.smoke import smoke_limit_cancel, smoke_roundtrip
from .models import KlineClosed, SignalParams
from .notify.alerts import AlertSink, export_day
from .timeutils import interval_to_ms, iso_now, parse_duration_seconds, utc_ms
from .waterfall import WaterfallEngine, waterfall_monitor, waterfall_shadow_collect
from .web import run_web


def cmd_live_preflight(args) -> int:
    settings = load_settings(args.config or args.settings)
    try:
        result = asyncio.run(
            live_preflight(
                settings,
                mode_override=args.mode,
                max_notional_override=args.max_notional_usdt,
            )
        )
    except (CredentialError, GatewayError, RuntimeError, ValueError) as exc:
        result = {
            "ok": False,
            "mode": args.mode,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "error_code": getattr(exc, "code", None),
            "http_status": getattr(exc, "status", None),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_live_issue_nonce(args) -> int:
    settings = load_settings(args.config or args.settings)
    config = LiveTradingConfig.from_settings(
        settings, mode_override=args.mode, max_notional_override=args.max_notional_usdt,
    )
    result = issue_order_nonce(config.ledger_path, ttl_seconds=args.ttl_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_live_run(args) -> int:
    settings = load_settings(args.config or args.settings)
    mode = str(args.mode)
    authorized = False
    if mode not in {"paper", "dry_run"}:
        authorization_nonce = (
            str(args.authorization_nonce or "").strip()
            or os.environ.pop("BINANCE_ORDER_AUTHORIZATION_NONCE", "").strip()
        )
        if not args.confirm_real_orders:
            raise RuntimeError("real-order mode requires --confirm-real-orders")
        if not authorization_nonce:
            raise RuntimeError("real-order mode requires --authorization-nonce")
        if args.max_notional_usdt is None:
            raise RuntimeError("real-order mode requires explicit --max-notional-usdt")
        config = LiveTradingConfig.from_settings(
            settings, mode_override=mode, max_notional_override=args.max_notional_usdt,
        )
        if not config.sends_real_orders:
            raise RuntimeError("live_trading.enabled and real_order_enabled must both be true")
        if not consume_order_nonce(config.ledger_path, authorization_nonce):
            raise RuntimeError("authorization nonce is invalid, expired, or already used")
        authorized = True

    async def runner() -> None:
        runtime = await build_runtime(
            settings,
            mode_override=mode,
            max_notional_override=args.max_notional_usdt,
            orders_authorized=authorized,
        )
        service = ClaudeLiveTradingService(
            settings, runtime, broad_top=args.broad_top, max_workers=args.max_workers,
        )
        try:
            await service.run(samples=args.samples)
        finally:
            await runtime.close()

    asyncio.run(runner())
    return 0


def _authorize_smoke(settings: dict[str, Any], args) -> LiveTradingConfig:
    if not args.confirm_real_orders:
        raise RuntimeError("smoke test requires --confirm-real-orders")
    config = LiveTradingConfig.from_settings(
        settings, mode_override=args.mode, max_notional_override=args.max_notional_usdt,
    )
    if not config.sends_real_orders:
        raise RuntimeError("live_trading.enabled and real_order_enabled must both be true")
    if not consume_order_nonce(config.ledger_path, args.authorization_nonce):
        raise RuntimeError("authorization nonce is invalid, expired, or already used")
    return config


def cmd_live_smoke_limit(args) -> int:
    settings = load_settings(args.config or args.settings)
    _authorize_smoke(settings, args)

    async def runner() -> dict[str, Any]:
        runtime = await build_runtime(
            settings, mode_override=args.mode,
            max_notional_override=args.max_notional_usdt, orders_authorized=True,
        )
        try:
            return await smoke_limit_cancel(runtime, args.symbol)
        finally:
            await runtime.close()

    print(json.dumps(asyncio.run(runner()), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_live_smoke_roundtrip(args) -> int:
    settings = copy.deepcopy(load_settings(args.config or args.settings))
    settings.setdefault("live_trading", {})["execution_policy"] = args.policy
    _authorize_smoke(settings, args)

    async def runner() -> dict[str, Any]:
        runtime = await build_runtime(
            settings, mode_override=args.mode,
            max_notional_override=args.max_notional_usdt, orders_authorized=True,
        )
        try:
            return await smoke_roundtrip(runtime, args.symbol)
        finally:
            await runtime.close()

    print(json.dumps(asyncio.run(runner()), ensure_ascii=False, indent=2, default=str))
    return 0


def make_context(args) -> tuple[dict[str, Any], Store, BinanceRestClient]:
    settings = load_settings(getattr(args, "config", None) or getattr(args, "settings", None))
    dirs = ensure_dirs(settings)
    return settings, Store(dirs["db"]), BinanceRestClient(settings)


def cmd_discover(args) -> int:
    settings, store, client = make_context(args)
    records, meta = discover_recent_liquidity(
        client,
        settings,
        top_n=args.top,
        broad_top=args.broad_top,
        now_ms=utc_ms(),
        params=SignalParams.from_dict(settings["params"]),
        max_workers=args.max_workers,
    )
    store.save_liquidity_snapshot(f"discover-{meta['data_cutoff_time']}", utc_ms(), records)
    engine = SignalEngine(settings)
    changed = engine.on_discovery(records, meta["data_cutoff_time"])
    store.upsert_pump_events(changed)
    selected = [r for r in records if r.selected]
    pumps = [r for r in records if r.pump_qualified]
    print(f"selected={len(selected)} pump={len(pumps)} cutoff={meta['data_cutoff_time']} errors={len(meta['errors'])}")
    print_discovery_sections(selected, pumps)
    if meta["errors"]:
        preview = "; ".join(f"{e['symbol']}={e['error']}" for e in meta["errors"][:5])
        print(f"errors_preview={preview}")
    return 0


def print_discovery_sections(selected, pumps) -> None:
    def row_text(row) -> str:
        state = "PUMP" if row.pump_qualified else "-"
        return (
            f"{row.symbol:>14} rank={row.rank:<4} pct15={row.pct_15m:+6.2f}% "
            f"pct30={row.pct_30m:+6.2f}% pct4h={row.pct_4h:+6.2f}% "
            f"pct12h={row.pct_12h:+6.2f}% pct1d={row.pct_1d:+6.2f}% qv15={row.quote_volume_15m/1e6:7.2f}M "
            f"qv30={row.quote_volume_30m/1e6:7.2f}M vr15={row.volume_ratio_15m:4.2f}x "
            f"vr30={row.volume_ratio_30m:4.2f}x {state}"
        )

    sections = [
        ("pump_qualified", sorted(pumps, key=lambda r: max(r.pct_15m, r.pct_30m, r.pct_4h, r.pct_12h, r.pct_1d), reverse=True)[:30]),
        ("top_15m_gainers", sorted(selected, key=lambda r: r.pct_15m, reverse=True)[:20]),
        ("top_30m_gainers", sorted(selected, key=lambda r: r.pct_30m, reverse=True)[:20]),
        ("recent_liquidity", sorted(selected, key=lambda r: r.rank)[:20]),
    ]
    for title, rows in sections:
        print(f"\n[{title}] count={len(rows)}")
        if not rows:
            print("  -")
            continue
        for row in rows:
            print(row_text(row))


def cmd_monitor(args) -> int:
    settings = load_settings(args.config or args.settings)
    active_strategy = str((settings.get("runtime") or {}).get("active_strategy", "waterfall_quant")).lower()
    if active_strategy in {
        "waterfall", "waterfall_quant", "waterfall_core", "waterfall_high_pf",
        "claude_board_wf_1m", "claude_champion",
    }:
        asyncio.run(
            waterfall_monitor(
                settings,
                broad_top=args.broad_top,
                discover_every=args.discover_every,
                samples=args.samples,
                max_workers=args.max_workers,
            )
        )
        return 0
    asyncio.run(
        monitor(
            settings,
            top_n=args.top,
            broad_top=args.broad_top,
            discover_every=args.discover_every,
            samples=args.samples,
            max_workers=args.max_workers,
        )
    )
    return 0


def cmd_backfill(args) -> int:
    settings, store, client = make_context(args)
    broad = build_broad_universe(client, settings, broad_top=args.broad_top)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else [r["symbol"] for r in broad]
    end = utc_ms()
    start = end - int(args.days) * 86_400_000
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    total = 0
    for symbol in symbols:
        for interval in intervals:
            total += backfill_symbol(client, store, symbol, interval, start, end)
            print(f"backfilled {symbol} {interval} total={total}", flush=True)
    return 0


def cmd_waterfall_monitor(args) -> int:
    settings = load_settings(args.config or args.settings)
    asyncio.run(
        waterfall_monitor(
            settings,
            broad_top=args.broad_top,
            discover_every=args.discover_every,
            samples=args.samples,
            max_workers=args.max_workers,
        )
    )
    return 0


def cmd_waterfall_download(args) -> int:
    settings, store, client = make_context(args)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        broad = build_broad_universe(client, settings, broad_top=args.broad_top)
        symbols = [str(r["symbol"]) for r in broad]
        if args.max_symbols > 0:
            symbols = symbols[: args.max_symbols]
    end = utc_ms()
    start = end - int(args.days) * 86_400_000
    total = 0
    for symbol in symbols:
        saved = backfill_symbol(client, store, symbol, "1m", start, end)
        total += saved
        print(f"waterfall downloaded {symbol} 1m saved={saved} total={total}", flush=True)
    print(json.dumps({"symbols": len(symbols), "candles": total, "days": args.days}, ensure_ascii=False, indent=2))
    return 0


def cmd_waterfall_replay(args) -> int:
    settings, store, _client = make_context(args)
    engine = WaterfallEngine(settings)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else store.candle_symbols("1m")
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    end = store.max_candle_close_time("1m", symbols=symbols)
    start = end - int(args.days) * 86_400_000 if args.days > 0 and end else None
    signals = []
    closed_positions = []
    processed = 0
    for symbol in symbols:
        candles = store.load_candles(symbol, "1m", start_time=start)
        for candle in candles:
            processed += 1
            watch, positions, emitted = engine.on_kline(KlineClosed(symbol, "1m", candle))
            signals.extend(emitted)
            for pos in positions:
                if pos.status == "closed":
                    closed_positions.append(pos)
            if args.save:
                store.upsert_waterfall_watch(watch)
                for pos in positions:
                    store.upsert_waterfall_position(pos.to_dict())
                for sig in emitted:
                    store.save_waterfall_signal(sig.to_dict())
        if args.progress_every and len(closed_positions) and len(closed_positions) % args.progress_every == 0:
            print(f"waterfall replay {symbol} processed={processed} trades={len(closed_positions)}", flush=True)
    metrics = summarize_waterfall_replay(closed_positions, signals, processed, len(symbols), args.days)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


def cmd_waterfall_shadow(args) -> int:
    settings = load_settings(args.config or args.settings)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    asyncio.run(
        waterfall_shadow_collect(
            settings,
            symbols=symbols,
            broad_top=args.broad_top,
            seconds=args.seconds,
            max_events=args.max_events,
        )
    )
    return 0


def summarize_waterfall_replay(closed_positions, signals, processed: int, symbols: int, days: int) -> dict[str, Any]:
    trades = list(closed_positions)
    gross_profit = sum(max(0.0, p.pnl_pct) for p in trades)
    gross_loss = -sum(min(0.0, p.pnl_pct) for p in trades)
    wins = sum(1 for p in trades if p.pnl_pct > 0)
    avg = sum((p.pnl_pct for p in trades), 0.0) / len(trades) if trades else 0.0
    avg_mfe = sum(((p.entry_price / p.best_price - 1.0) for p in trades if p.best_price > 0), 0.0) / len(trades) if trades else 0.0
    avg_mae = sum(((p.worst_price / p.entry_price - 1.0) for p in trades if p.entry_price > 0), 0.0) / len(trades) if trades else 0.0
    return {
        "strategy": "waterfall_quant",
        "symbols": symbols,
        "days": days,
        "candles_processed": processed,
        "signals": len(signals),
        "trades": len(trades),
        "trades_per_day": len(trades) / max(1, days),
        "win_rate": wins / len(trades) if trades else 0.0,
        "avg_pnl_pct": avg,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": avg_mae,
        "total_pnl_pct_units": sum((p.pnl_pct for p in trades), 0.0),
        "by_exit_reason": exit_reason_counts(trades),
    }


def exit_reason_counts(positions) -> dict[str, int]:
    out: dict[str, int] = {}
    for pos in positions:
        out[pos.exit_reason] = out.get(pos.exit_reason, 0) + 1
    return out


def cmd_import_bb_data(args) -> int:
    settings, store, _client = make_context(args)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    summary = import_bb_klines(
        store=store,
        settings=settings,
        source=args.source,
        days=args.days,
        max_symbols=args.max_symbols,
        symbols=symbols,
        rank_window_days=args.rank_window_days,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0


def backfill_symbol(client: BinanceRestClient, store: Store, symbol: str, interval: str, start: int, end: int) -> int:
    step = interval_to_ms(interval)
    cursor = start
    saved = 0
    while cursor < end:
        candles = client.klines(symbol, interval, limit=1500, start_time=cursor, end_time=end)
        candles = [c for c in candles if c.close_time < end]
        if not candles:
            break
        saved += store.save_candles(candles)
        cursor = candles[-1].open_time + step
        if len(candles) < 1500:
            break
    return saved


def cmd_backtest(args) -> int:
    settings, store, _client = make_context(args)
    params_data = dict(settings["params"])
    if args.param_overrides:
        params_data.update(parse_param_overrides(args.param_overrides))
    result = run_backtest_from_store(
        store,
        settings,
        SignalParams.from_dict(params_data),
        days=args.days,
        top_n=args.top,
    )
    run_id = f"backtest-{utc_ms()}"
    store.save_backtest_run(run_id, iso_now(), args.days, result.params.to_dict(), result.metrics, None, None, None, None)
    metrics = result.metrics if args.details else {k: v for k, v in result.metrics.items() if k != "rows"}
    print(json.dumps({"run_id": run_id, "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0


def parse_param_overrides(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    out: dict[str, Any] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        key, value = part.split("=", 1)
        value = value.strip()
        try:
            parsed: Any = float(value)
        except ValueError:
            parsed = value
        out[key.strip()] = parsed
    return out


def cmd_optimize(args) -> int:
    settings, store, _client = make_context(args)
    results = optimize_from_store(store, settings, args.param_grid, days=args.days, top_n=args.top)
    if not args.details:
        results = [
            {**row, "metrics": {k: v for k, v in row["metrics"].items() if k != "rows"}}
            for row in results
        ]
    print(json.dumps(results[:20], ensure_ascii=False, indent=2))
    return 0


def cmd_status(args) -> int:
    settings, store, _client = make_context(args)
    active = store.active_pump_events(utc_ms())
    alerts = store.recent_alerts(limit=args.limit)
    print(f"active_pump_events={len(active)}")
    for event in active[: args.limit]:
        print(f"{event.symbol:>14} high={event.high_price} current={event.current_price} expires={event.expires_at}")
    print(f"recent_alerts={len(alerts)}")
    for alert in alerts:
        print(f"{alert['level']} {alert['symbol']} price={alert['price']} time={alert['decision_time']}")
    return 0


def cmd_export(args) -> int:
    settings = load_settings(args.config or args.settings)
    dirs = ensure_dirs(settings)
    path = export_day(dirs["alerts"], args.date)
    print(path)
    return 0


def cmd_replay_alert(args) -> int:
    settings, store, _client = make_context(args)
    row = store.get_alert(args.alert_id)
    if not row:
        print(f"alert not found: {args.alert_id}")
        return 1
    event = store.get_pump_event(row["event_id"])
    if not event:
        print(f"pump event not found: {row['event_id']}")
        return 1
    engine = SignalEngine(settings)
    engine.load_events([event])
    sink = AlertSink(Path(settings["paths"]["alerts_dir"]))
    symbol = row["symbol"]
    cutoff = int(row["source_candle_close_time"])
    candles = []
    candles.extend(store.load_candles(symbol, "1m", end_time=cutoff))
    candles.extend(store.load_candles(symbol, "5m", end_time=cutoff))
    candles.sort(key=lambda c: (c.close_time, c.interval))
    replayed = []
    for candle in candles:
        _changed, alerts = engine.on_kline(KlineClosed(candle.symbol, candle.interval, candle))
        replayed.extend(alerts)
    matches = [a for a in replayed if a.alert_id == args.alert_id]
    print(json.dumps({"target": row, "replayed_match": bool(matches), "replayed_count": len(replayed)}, ensure_ascii=False, indent=2))
    if args.emit and matches:
        emit_alerts(store, sink, matches)
    return 0 if matches else 2



def cmd_web(args) -> int:
    settings = load_settings(args.config or args.settings)
    run_web(settings, host=args.host, port=args.port)
    return 0

def cmd_collect_micro(args) -> int:
    from .micro_collector import collect_micro

    settings = load_settings(args.config or args.settings)
    asyncio.run(collect_micro(
        settings,
        broad_top=args.broad_top,
        oi_interval=args.oi_interval,
        depth_top=args.depth_top,
        depth_interval=args.depth_interval,
        retention_days=args.retention_days,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance pump-dump short signal hunter")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    micro = sub.add_parser("collect-micro", help="collect OI / liquidation / hot-pool depth micro data")
    micro.add_argument("--config", default=None)
    micro.add_argument("--broad-top", type=int, default=450)
    micro.add_argument("--oi-interval", type=int, default=60)
    micro.add_argument("--depth-top", type=int, default=60)
    micro.add_argument("--depth-interval", type=int, default=30)
    micro.add_argument("--retention-days", type=int, default=90)
    micro.set_defaults(func=cmd_collect_micro)

    discover = sub.add_parser("discover")
    discover.add_argument("--config", default=None)
    discover.add_argument("--top", type=int, default=250)
    discover.add_argument("--broad-top", type=int, default=None)
    discover.add_argument("--scan-window", default="15m,30m")
    discover.add_argument("--max-workers", type=int, default=12)
    discover.set_defaults(func=cmd_discover)

    mon = sub.add_parser("monitor")
    mon.add_argument("--config", default=None)
    mon.add_argument("--top", type=int, default=250)
    mon.add_argument("--broad-top", type=int, default=None)
    mon.add_argument("--discover-every", default="15m")
    mon.add_argument("--samples", type=int, default=0)
    mon.add_argument("--max-workers", type=int, default=12)
    mon.set_defaults(func=cmd_monitor)

    wfmon = sub.add_parser("waterfall-monitor")
    wfmon.add_argument("--config", default=None)
    wfmon.add_argument("--broad-top", type=int, default=None)
    wfmon.add_argument("--discover-every", default=None)
    wfmon.add_argument("--samples", type=int, default=0)
    wfmon.add_argument("--max-workers", type=int, default=None)
    wfmon.set_defaults(func=cmd_waterfall_monitor)

    wfdown = sub.add_parser("waterfall-download")
    wfdown.add_argument("--config", default=None)
    wfdown.add_argument("--days", type=int, default=7)
    wfdown.add_argument("--broad-top", type=int, default=450)
    wfdown.add_argument("--max-symbols", type=int, default=80)
    wfdown.add_argument("--symbols", default="")
    wfdown.set_defaults(func=cmd_waterfall_download)

    wfreplay = sub.add_parser("waterfall-replay")
    wfreplay.add_argument("--config", default=None)
    wfreplay.add_argument("--days", type=int, default=14)
    wfreplay.add_argument("--symbols", default="")
    wfreplay.add_argument("--max-symbols", type=int, default=0)
    wfreplay.add_argument("--save", action="store_true")
    wfreplay.add_argument("--progress-every", type=int, default=100)
    wfreplay.set_defaults(func=cmd_waterfall_replay)

    wfshadow = sub.add_parser("waterfall-shadow")
    wfshadow.add_argument("--config", default=None)
    wfshadow.add_argument("--symbols", default="")
    wfshadow.add_argument("--broad-top", type=int, default=80)
    wfshadow.add_argument("--seconds", type=int, default=600)
    wfshadow.add_argument("--max-events", type=int, default=0)
    wfshadow.set_defaults(func=cmd_waterfall_shadow)

    live_pf = sub.add_parser("live-preflight", help="read-only Binance account and API preflight")
    live_pf.add_argument("--config", default=None)
    live_pf.add_argument("--mode", choices=["dry_run", "testnet", "live_micro", "live"], default="dry_run")
    live_pf.add_argument("--max-notional-usdt", type=float, default=None)
    live_pf.set_defaults(func=cmd_live_preflight)

    live_nonce = sub.add_parser("live-issue-nonce", help="issue a one-time short-lived real-order authorization")
    live_nonce.add_argument("--config", default=None)
    live_nonce.add_argument("--mode", choices=["testnet", "live_micro", "live"], default="live_micro")
    live_nonce.add_argument("--max-notional-usdt", type=float, required=True)
    live_nonce.add_argument("--ttl-seconds", type=int, default=300)
    live_nonce.set_defaults(func=cmd_live_issue_nonce)

    live_run = sub.add_parser("live-run", help="run isolated Claude live/dry execution service")
    live_run.add_argument("--config", default=None)
    live_run.add_argument("--mode", choices=["dry_run", "testnet", "live_micro", "live"], default="dry_run")
    live_run.add_argument("--max-notional-usdt", type=float, default=None)
    live_run.add_argument("--broad-top", type=int, default=None)
    live_run.add_argument("--max-workers", type=int, default=None)
    live_run.add_argument("--samples", type=int, default=0)
    live_run.add_argument("--confirm-real-orders", action="store_true")
    live_run.add_argument("--authorization-nonce", default="")
    live_run.set_defaults(func=cmd_live_run)

    for name, help_text, func in (
        ("live-smoke-limit", "real post-only limit/cancel protocol smoke", cmd_live_smoke_limit),
        ("live-smoke-roundtrip", "real minimum short open/protect/close smoke", cmd_live_smoke_roundtrip),
    ):
        smoke = sub.add_parser(name, help=help_text)
        smoke.add_argument("--config", default=None)
        smoke.add_argument("--mode", choices=["testnet", "live_micro"], default="live_micro")
        smoke.add_argument("--symbol", required=True)
        smoke.add_argument("--max-notional-usdt", type=float, required=True)
        smoke.add_argument("--confirm-real-orders", action="store_true")
        smoke.add_argument("--authorization-nonce", required=True)
        if name == "live-smoke-roundtrip":
            smoke.add_argument("--policy", choices=["market", "ioc", "maker_first"], required=True)
        smoke.set_defaults(func=func)

    bf = sub.add_parser("backfill")
    bf.add_argument("--config", default=None)
    bf.add_argument("--days", type=int, default=60)
    bf.add_argument("--broad-top", type=int, default=500)
    bf.add_argument("--symbols", default="")
    bf.add_argument("--intervals", default="1m,5m")
    bf.set_defaults(func=cmd_backfill)

    imp = sub.add_parser("import-bb-data")
    imp.add_argument("--config", default=None)
    imp.add_argument("--source", default=r"E:\A\bb\data")
    imp.add_argument("--days", type=int, default=60)
    imp.add_argument("--max-symbols", type=int, default=80)
    imp.add_argument("--symbols", default="")
    imp.add_argument("--rank-window-days", type=int, default=7)
    imp.set_defaults(func=cmd_import_bb_data)

    bt = sub.add_parser("backtest")
    bt.add_argument("--config", default=None)
    bt.add_argument("--days", type=int, default=60)
    bt.add_argument("--top", type=int, default=250)
    bt.add_argument("--details", action="store_true")
    bt.add_argument("--param-overrides", default="")
    bt.set_defaults(func=cmd_backtest)

    opt = sub.add_parser("optimize")
    opt.add_argument("--config", default=None)
    opt.add_argument("--days", type=int, default=60)
    opt.add_argument("--top", type=int, default=250)
    opt.add_argument("--param-grid", default="config/param_grid.json")
    opt.add_argument("--details", action="store_true")
    opt.set_defaults(func=cmd_optimize)

    status = sub.add_parser("status")
    status.add_argument("--config", default=None)
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=cmd_status)

    export = sub.add_parser("export")
    export.add_argument("--config", default=None)
    export.add_argument("--date", required=True)
    export.set_defaults(func=cmd_export)

    replay = sub.add_parser("replay-alert")
    replay.add_argument("--config", default=None)
    replay.add_argument("--alert-id", required=True)
    replay.add_argument("--emit", action="store_true")
    replay.set_defaults(func=cmd_replay_alert)
    web = sub.add_parser("web")
    web.add_argument("--config", default=None)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8787)
    web.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
