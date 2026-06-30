from __future__ import annotations

import argparse
import asyncio
import json
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
from .models import KlineClosed, SignalParams
from .notify.alerts import AlertSink, export_day
from .timeutils import interval_to_ms, iso_now, parse_duration_seconds, utc_ms
from .web import run_web


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

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance pump-dump short signal hunter")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

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
