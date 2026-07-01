from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import ensure_dirs
from .data.rest_client import BinanceRestClient
from .data.store import Store
from .data.websocket_source import WebSocketMarketSource
from .discovery import discover_recent_liquidity
from .engine.signal_engine import SignalEngine
from .models import Alert, SignalParams
from .notify.alerts import AlertSink
from .timeutils import closed_candle_cutoff_ms, local_stamp, parse_duration_seconds, utc_ms


async def monitor(
    settings: dict[str, Any],
    top_n: int,
    broad_top: int | None,
    discover_every: str,
    samples: int = 0,
    max_workers: int = 12,
) -> None:
    dirs = ensure_dirs(settings)
    store = Store(dirs["db"])
    client = BinanceRestClient(settings)
    sink = AlertSink(dirs["alerts"])
    engine = SignalEngine(settings)
    engine.load_events(store.active_pump_events(utc_ms()))
    if engine.long_enabled:
        engine.load_long_events(store.active_long_events(utc_ms()))
    print(f"signal mode={engine.mode} new_high_reset={engine.params.new_high_reset_pct}%", flush=True)
    discover_seconds = parse_duration_seconds(discover_every)
    processed = 0

    selected = run_discovery_cycle(client, store, engine, settings, top_n, broad_top, max_workers)
    prewarm_active_events(client, store, engine, settings)
    watch_symbols = build_watch_symbols(engine, selected)
    source = WebSocketMarketSource(settings, watch_symbols, settings["websocket"]["intervals"])
    agen = source.events()
    next_discovery = utc_ms() + discover_seconds * 1000
    print(f"websocket streams={len(source.stream_names())} symbols={len(watch_symbols)}", flush=True)

    try:
        while samples <= 0 or processed < samples:
            timeout = max(1.0, (next_discovery - utc_ms()) / 1000.0)
            try:
                event = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
            except asyncio.TimeoutError:
                await source.close()
                await agen.aclose()
                selected = run_discovery_cycle(client, store, engine, settings, top_n, broad_top, max_workers)
                prewarm_active_events(client, store, engine, settings)
                watch_symbols = build_watch_symbols(engine, selected)
                source = WebSocketMarketSource(settings, watch_symbols, settings["websocket"]["intervals"])
                agen = source.events()
                next_discovery = utc_ms() + discover_seconds * 1000
                print(f"websocket streams={len(source.stream_names())} symbols={len(watch_symbols)}", flush=True)
                continue
            processed += 1
            store.save_candles([event.candle])
            changed, alerts = engine.on_kline(event)
            store.upsert_pump_events(changed)
            if engine.long_enabled:
                le = engine.long_events_by_symbol.get(event.symbol)
                if le is not None:
                    store.upsert_long_events([le])
            emit_alerts(store, sink, alerts)
            if processed % int(settings["websocket"].get("heartbeat_events", 250)) == 0:
                print(
                    f"[{local_stamp()}] monitor events={processed} last={event.symbol} {event.interval} "
                    f"active={len(active_event_symbols(engine))}",
                    flush=True,
                )
    finally:
        await source.close()
        await agen.aclose()
        print(f"[{local_stamp()}] monitor stopped events={processed}", flush=True)


def run_discovery_cycle(
    client: BinanceRestClient,
    store: Store,
    engine: SignalEngine,
    settings: dict[str, Any],
    top_n: int,
    broad_top: int | None,
    max_workers: int,
) -> list[str]:
    now = utc_ms()
    print(f"[{local_stamp()}] discovery start top={top_n} broad_top={broad_top or settings['universe']['broad_top']}", flush=True)
    records, meta = discover_recent_liquidity(
        client,
        settings,
        top_n=top_n,
        broad_top=broad_top,
        now_ms=now,
        params=SignalParams.from_dict(settings["params"]),
        max_workers=max_workers,
    )
    run_id = f"discover-{meta['data_cutoff_time']}"
    store.save_liquidity_snapshot(run_id, now, records)
    changed = engine.on_discovery(records, meta["data_cutoff_time"])
    store.upsert_pump_events(changed)
    if engine.long_enabled:
        try:
            refresh_long_flow(client, engine, max_workers)
        except Exception as exc:
            print(f"[{local_stamp()}] long flow refresh failed: {type(exc).__name__}: {exc}", flush=True)
        store.upsert_long_events(list(engine.long_events_by_symbol.values()))
    selected = [r.symbol for r in records if r.selected]
    long_cands = sum(1 for r in records if r.long_candidate)
    print(
        f"[{local_stamp()}] discovery selected={len(selected)} pump={meta['pump_count']} "
        f"long_cand={long_cands} long_watch={len(engine.active_long_symbols())} "
        f"errors={len(meta['errors'])} cutoff={meta['data_cutoff_time']}",
        flush=True,
    )
    pumps = [r for r in records if r.pump_qualified]
    if pumps:
        preview = ", ".join(
            f"{r.symbol}(30m={r.pct_30m:+.2f}%,4h={r.pct_4h:+.2f}%,12h={r.pct_12h:+.2f}%,1d={r.pct_1d:+.2f}%)"
            for r in sorted(pumps, key=lambda x: max(x.pct_15m, x.pct_30m, x.pct_4h, x.pct_12h, x.pct_1d), reverse=True)[:12]
        )
        print(f"[{local_stamp()}] pump watch: {preview}", flush=True)
    if meta["errors"]:
        preview = "; ".join(f"{e['symbol']}={e['error']}" for e in meta["errors"][:5])
        print(f"[{local_stamp()}] discovery error preview: {preview}", flush=True)
    return selected


def active_event_symbols(engine: SignalEngine) -> list[str]:
    now = utc_ms()
    return sorted(
        event.symbol
        for event in engine.events_by_symbol.values()
        if event.status == "active"
        and event.expires_at >= now
        and event.symbol.isascii()
        and event.symbol.isalnum()
    )


def build_watch_symbols(engine: SignalEngine, selected: list[str]) -> list[str]:
    return sorted(set(selected) | set(active_event_symbols(engine)) | set(engine.active_long_symbols()))


def prewarm_active_events(client: BinanceRestClient, store: Store, engine: SignalEngine, settings: dict[str, Any]) -> None:
    symbols = sorted(set(active_event_symbols(engine)) | set(engine.active_long_symbols()))
    if not symbols:
        return
    intervals = list(settings["websocket"]["intervals"])
    limit = int(settings["websocket"].get("prewarm_limit", 80))
    total = 0
    errors: list[str] = []
    changed = []
    for symbol in symbols:
        for interval in intervals:
            try:
                cutoff = closed_candle_cutoff_ms(utc_ms(), interval)
                candles = [c for c in client.klines(symbol, interval, limit=limit) if c.close_time <= cutoff]
                if not candles:
                    continue
                store.save_candles(candles)
                changed.extend(engine.prime_candles(candles))
                total += len(candles)
            except Exception as exc:
                errors.append(f"{symbol}/{interval}={type(exc).__name__}: {exc}"[:160])
    store.upsert_pump_events(changed)
    print(
        f"[{local_stamp()}] prewarm active_symbols={len(symbols)} candles={total} errors={len(errors)}",
        flush=True,
    )
    if errors:
        print(f"[{local_stamp()}] prewarm error preview: {'; '.join(errors[:5])}", flush=True)


def refresh_long_flow(client: BinanceRestClient, engine: SignalEngine, max_workers: int = 8) -> None:
    """每 15m 为做多监管币拉取 OI/多空/taker, 刷新引擎资金流缓存(供 long 模型完整 94 特征打分)。"""
    symbols = engine.active_long_symbols()
    if not symbols:
        return
    import pandas as pd

    def fetch(sym: str):
        oi = client.open_interest_hist(sym, "15m", 200)
        lsg = client.global_long_short_ratio(sym, "15m", 200)
        lstp = client.top_position_ratio(sym, "15m", 200)
        tkr = client.taker_long_short_ratio(sym, "15m", 200)
        df = pd.DataFrame(oi, columns=["ts", "oi", "oival"])
        for rows, col in ((lsg, "lsg"), (lstp, "lstp"), (tkr, "tkr")):
            d = pd.DataFrame(rows, columns=["ts", col])
            df = pd.merge_asof(df.sort_values("ts"), d.sort_values("ts"), on="ts", direction="backward")
        return sym, df

    ok = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch, s): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                s, df = fut.result()
                engine.set_flow(s, df)
                ok += 1
            except Exception as exc:
                errors.append(f"{sym}={type(exc).__name__}: {exc}"[:120])
    print(f"[{local_stamp()}] long flow refreshed {ok}/{len(symbols)} errors={len(errors)}", flush=True)
    if errors:
        print(f"[{local_stamp()}] long flow error preview: {'; '.join(errors[:5])}", flush=True)


def emit_alerts(store: Store, sink: AlertSink, alerts: list[Alert]) -> None:
    for alert in alerts:
        pushed, push_msg = sink.emit(alert)
        store.save_alert(alert, pushed=pushed, push_error="" if pushed else push_msg)
