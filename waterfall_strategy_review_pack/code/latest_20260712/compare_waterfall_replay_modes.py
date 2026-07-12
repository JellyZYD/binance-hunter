"""Compare closed-1m waterfall replay with aggTrade partial-candle replay.

The comparison uses the same symbols, date range, rules and exit profiles.
It is CPU/IO bound; multiprocessing by symbol is the main speedup path.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pump_dump_hunter.models import Candle, KlineClosed
from pump_dump_hunter.waterfall import WaterfallEngine, WaterfallPosition, evidence_float

from ml_experiments.replay_aggtrade_waterfall import iter_symbol_trades, partial_to_candle


MINUTE_MS = 60_000


def main() -> int:
    args = parse_args()
    symbols = discover_symbols(Path(args.agg_dir), args.symbols, args.max_symbols, args.symbol_order)
    settings = load_settings_json(args.config)
    if args.variant:
        settings.setdefault("waterfall_quant", {})["variant"] = args.variant
    if args.families:
        settings.setdefault("waterfall_quant", {})["enabled_families"] = [
            x.strip() for x in args.families.split(",") if x.strip()
        ]
    jobs = [(symbol, settings, vars(args)) for symbol in symbols]
    rows: list[dict[str, Any]] = []
    print(json.dumps({"symbols": len(symbols), "workers": args.workers, "start": args.start, "end": args.end}, ensure_ascii=False), flush=True)
    if args.workers <= 1:
        for job in jobs:
            rows.append(run_symbol_compare(job))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run_symbol_compare, job): job[0] for job in jobs}
            done = 0
            for fut in as_completed(futs):
                rows.append(fut.result())
                done += 1
                if done % max(1, args.progress_every) == 0:
                    print(f"compared {done}/{len(jobs)}", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail_path = out_dir / f"waterfall_mode_compare_detail_{stamp}.csv"
    write_dicts(detail_path, rows)
    metrics = {
        "start": args.start,
        "end": args.end,
        "symbols": len(symbols),
        "variant": args.variant or settings.get("waterfall_quant", {}).get("variant", "core"),
        "families": settings.get("waterfall_quant", {}).get("enabled_families", []),
        "kline": summarize_rows(rows, "kline"),
        "agg": summarize_rows(rows, "agg"),
        "hybrid_full": summarize_rows(rows, "hybrid_full"),
        "hybrid_stop": summarize_rows(rows, "hybrid_stop"),
        "hybrid_guard08": summarize_rows(rows, "hybrid_guard08"),
        "hybrid_guard12": summarize_rows(rows, "hybrid_guard12"),
        "hybrid_guard16": summarize_rows(rows, "hybrid_guard16"),
        "hybrid_preclose": summarize_rows(rows, "hybrid_preclose"),
        "hybrid_micro_sell60": summarize_rows(rows, "hybrid_micro_sell60"),
        "hybrid_micro_strong": summarize_rows(rows, "hybrid_micro_strong"),
        "detail_path": str(detail_path),
    }
    metrics_path = out_dir / f"waterfall_mode_compare_metrics_{stamp}.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="backend/config/settings.json")
    p.add_argument("--agg-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--klines-dir", default=r"E:\A\bb\data\klines")
    p.add_argument("--out-dir", default="backend/storage/ml/waterfall_mode_compare")
    p.add_argument("--symbols", default="")
    p.add_argument("--max-symbols", type=int, default=60)
    p.add_argument("--symbol-order", choices=["name", "size_asc", "size_desc"], default="name")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--variant", choices=["core", "high_pf"], default="core")
    p.add_argument("--families", default="post_pump,downtrend_continuation,momentum_dump")
    p.add_argument("--prewarm", type=int, default=1500)
    p.add_argument("--eval-ms", type=int, default=1000)
    p.add_argument("--preclose-age-ms", type=int, default=45_000)
    p.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 1)))
    p.add_argument("--progress-every", type=int, default=5)
    return p.parse_args()


def run_symbol_compare(job: tuple[str, dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    symbol, settings, args = job
    start_ms = int(datetime.fromisoformat(args["start"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int((datetime.fromisoformat(args["end"]).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1
    klines = load_klines(symbol, Path(args["klines_dir"]), start_ms, end_ms, int(args["prewarm"]))
    kline_positions, kline_signals, kline_candles = replay_kline(symbol, settings, klines, start_ms, end_ms, int(args["prewarm"]))
    (
        agg_positions,
        agg_signals,
        hybrid_full_positions,
        hybrid_full_signals,
        hybrid_stop_positions,
        hybrid_stop_signals,
        hybrid_guard08_positions,
        hybrid_guard08_signals,
        hybrid_guard12_positions,
        hybrid_guard12_signals,
        hybrid_guard16_positions,
        hybrid_guard16_signals,
        hybrid_preclose_positions,
        hybrid_preclose_signals,
        hybrid_micro_sell60_positions,
        hybrid_micro_sell60_signals,
        hybrid_micro_strong_positions,
        hybrid_micro_strong_signals,
        agg_trades,
    ) = replay_agg_and_hybrid(
        symbol,
        settings,
        klines,
        Path(args["agg_dir"]),
        args,
        start_ms,
        end_ms,
    )
    row = {
        "symbol": symbol,
        "_days": (date.fromisoformat(args["end"]) - date.fromisoformat(args["start"])).days + 1,
        "kline_candles": kline_candles,
        "agg_trades": agg_trades,
        "hybrid_full_trades_processed": agg_trades,
        "hybrid_stop_trades_processed": agg_trades,
        "hybrid_guard08_trades_processed": agg_trades,
        "hybrid_guard12_trades_processed": agg_trades,
        "hybrid_guard16_trades_processed": agg_trades,
        "hybrid_preclose_trades_processed": agg_trades,
        "hybrid_micro_sell60_trades_processed": agg_trades,
        "hybrid_micro_strong_trades_processed": agg_trades,
        **prefix_metrics("kline", summarize_positions(kline_positions, kline_signals)),
        **prefix_metrics("agg", summarize_positions(agg_positions, agg_signals)),
        **prefix_metrics("hybrid_full", summarize_positions(hybrid_full_positions, hybrid_full_signals)),
        **prefix_metrics("hybrid_stop", summarize_positions(hybrid_stop_positions, hybrid_stop_signals)),
        **prefix_metrics("hybrid_guard08", summarize_positions(hybrid_guard08_positions, hybrid_guard08_signals)),
        **prefix_metrics("hybrid_guard12", summarize_positions(hybrid_guard12_positions, hybrid_guard12_signals)),
        **prefix_metrics("hybrid_guard16", summarize_positions(hybrid_guard16_positions, hybrid_guard16_signals)),
        **prefix_metrics("hybrid_preclose", summarize_positions(hybrid_preclose_positions, hybrid_preclose_signals)),
        **prefix_metrics("hybrid_micro_sell60", summarize_positions(hybrid_micro_sell60_positions, hybrid_micro_sell60_signals)),
        **prefix_metrics("hybrid_micro_strong", summarize_positions(hybrid_micro_strong_positions, hybrid_micro_strong_signals)),
    }
    return row


def replay_kline(
    symbol: str,
    settings: dict[str, Any],
    rows: list[Candle],
    start_ms: int,
    end_ms: int,
    prewarm: int,
) -> tuple[list[WaterfallPosition], list[dict[str, Any]], int]:
    engine = WaterfallEngine(settings)
    if not rows:
        return [], [], 0
    pre = [c for c in rows if c.close_time < start_ms][-prewarm:]
    engine.prime_candles(pre)
    positions: list[WaterfallPosition] = []
    signals: list[dict[str, Any]] = []
    count = 0
    for candle in rows:
        if candle.close_time < start_ms or candle.close_time > end_ms:
            continue
        count += 1
        _watch, changed, emitted = engine.on_kline(KlineClosed(symbol, "1m", candle))
        for pos in changed:
            if pos.status == "closed":
                positions.append(pos)
        signals.extend(s.to_dict() for s in emitted)
    return positions, signals, count


def replay_agg_and_hybrid(
    symbol: str,
    settings: dict[str, Any],
    rows: list[Candle],
    agg_dir: Path,
    args: dict[str, Any],
    start_ms: int,
    end_ms: int,
) -> tuple[
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    list[WaterfallPosition],
    list[dict[str, Any]],
    int,
]:
    agg_engine = WaterfallEngine(settings)
    hybrid_full_engine = WaterfallEngine(settings)
    hybrid_stop_engine = WaterfallEngine(settings)
    hybrid_guard08_engine = WaterfallEngine(settings)
    hybrid_guard12_engine = WaterfallEngine(settings)
    hybrid_guard16_engine = WaterfallEngine(settings)
    hybrid_preclose_engine = WaterfallEngine(settings)
    hybrid_micro_sell60_engine = WaterfallEngine(settings)
    hybrid_micro_strong_engine = WaterfallEngine(settings)
    prewarm = int(args["prewarm"])
    pre = [c for c in rows if c.close_time < start_ms][-prewarm:]
    period = [c for c in rows if start_ms <= c.close_time <= end_ms]
    agg_engine.prime_candles(pre)
    hybrid_full_engine.prime_candles(pre)
    hybrid_stop_engine.prime_candles(pre)
    hybrid_guard08_engine.prime_candles(pre)
    hybrid_guard12_engine.prime_candles(pre)
    hybrid_guard16_engine.prime_candles(pre)
    hybrid_preclose_engine.prime_candles(pre)
    hybrid_micro_sell60_engine.prime_candles(pre)
    hybrid_micro_strong_engine.prime_candles(pre)
    agg_positions: list[WaterfallPosition] = []
    agg_signals: list[dict[str, Any]] = []
    hybrid_full_positions: list[WaterfallPosition] = []
    hybrid_full_signals: list[dict[str, Any]] = []
    hybrid_stop_positions: list[WaterfallPosition] = []
    hybrid_stop_signals: list[dict[str, Any]] = []
    hybrid_guard08_positions: list[WaterfallPosition] = []
    hybrid_guard08_signals: list[dict[str, Any]] = []
    hybrid_guard12_positions: list[WaterfallPosition] = []
    hybrid_guard12_signals: list[dict[str, Any]] = []
    hybrid_guard16_positions: list[WaterfallPosition] = []
    hybrid_guard16_signals: list[dict[str, Any]] = []
    hybrid_preclose_positions: list[WaterfallPosition] = []
    hybrid_preclose_signals: list[dict[str, Any]] = []
    hybrid_micro_sell60_positions: list[WaterfallPosition] = []
    hybrid_micro_sell60_signals: list[dict[str, Any]] = []
    hybrid_micro_strong_positions: list[WaterfallPosition] = []
    hybrid_micro_strong_signals: list[dict[str, Any]] = []
    processed = 0
    candle_idx = 0
    partial: dict[str, Any] | None = None
    closed_micro: dict[int, dict[str, float]] = {}
    last_eval = 0
    last_preclose_eval = 0
    start_day = date.fromisoformat(args["start"])
    end_day = date.fromisoformat(args["end"])
    for trade in iter_symbol_trades(agg_dir, symbol, start_day, end_day):
        ts = int(trade["time"])
        if ts < start_ms or ts > end_ms:
            continue
        processed += 1
        price = float(trade["price"])
        qty = float(trade["qty"])
        quote = price * qty

        minute = ts - (ts % MINUTE_MS)
        if partial is None or partial["open_time"] != minute:
            if partial is not None:
                closed_micro[int(partial["open_time"])] = micro_from_partial(partial)
                agg_engine._append(partial_to_candle(symbol, partial, partial["open_time"] + MINUTE_MS - 1))
            partial = {
                "open_time": minute,
                "open": price,
                "high": price,
                "low": price,
                "low_sec": max(0, min(59, int((ts - minute) / 1000))),
                "close": price,
                "volume": qty,
                "quote_volume": quote,
                "trades": 1,
                "taker_buy_quote": 0.0 if trade["buyer_maker"] else quote,
                "taker_buy_base": 0.0 if trade["buyer_maker"] else qty,
                "sell_quote": quote if trade["buyer_maker"] else 0.0,
                "last10_quote": quote if ts - minute >= 50_000 else 0.0,
                "last10_sell_quote": quote if trade["buyer_maker"] and ts - minute >= 50_000 else 0.0,
                "price_50s": price if ts - minute >= 50_000 else 0.0,
            }
        else:
            partial["high"] = max(float(partial["high"]), price)
            if price <= float(partial["low"]):
                partial["low"] = price
                partial["low_sec"] = max(0, min(59, int((ts - minute) / 1000)))
            partial["close"] = price
            partial["volume"] += qty
            partial["quote_volume"] += quote
            partial["trades"] += 1
            if not trade["buyer_maker"]:
                partial["taker_buy_quote"] += quote
                partial["taker_buy_base"] += qty
            else:
                partial["sell_quote"] += quote
            if ts - minute >= 50_000:
                partial["last10_quote"] += quote
                if trade["buyer_maker"]:
                    partial["last10_sell_quote"] += quote
                if not partial["price_50s"]:
                    partial["price_50s"] = price

        while candle_idx < len(period) and period[candle_idx].close_time <= ts:
            candle = period[candle_idx]
            candle_idx += 1
            micro = closed_micro.get(candle.open_time, {})
            _watch, changed, emitted = hybrid_full_engine.on_kline(KlineClosed(symbol, "1m", candle))
            for pos in changed:
                if pos.status == "closed":
                    hybrid_full_positions.append(pos)
            hybrid_full_signals.extend(s.to_dict() for s in emitted)
            _watch, changed, emitted = hybrid_stop_engine.on_kline(KlineClosed(symbol, "1m", candle))
            for pos in changed:
                if pos.status == "closed":
                    hybrid_stop_positions.append(pos)
            hybrid_stop_signals.extend(s.to_dict() for s in emitted)
            _watch, changed, emitted = hybrid_guard08_engine.on_kline(KlineClosed(symbol, "1m", candle))
            for pos in changed:
                if pos.status == "closed":
                    hybrid_guard08_positions.append(pos)
            hybrid_guard08_signals.extend(s.to_dict() for s in emitted)
            _watch, changed, emitted = hybrid_guard12_engine.on_kline(KlineClosed(symbol, "1m", candle))
            for pos in changed:
                if pos.status == "closed":
                    hybrid_guard12_positions.append(pos)
            hybrid_guard12_signals.extend(s.to_dict() for s in emitted)
            _watch, changed, emitted = hybrid_guard16_engine.on_kline(KlineClosed(symbol, "1m", candle))
            for pos in changed:
                if pos.status == "closed":
                    hybrid_guard16_positions.append(pos)
            hybrid_guard16_signals.extend(s.to_dict() for s in emitted)
            _watch, changed, emitted = hybrid_preclose_engine.on_kline(KlineClosed(symbol, "1m", candle))
            for pos in changed:
                if pos.status == "closed":
                    hybrid_preclose_positions.append(pos)
            hybrid_preclose_signals.extend(s.to_dict() for s in emitted)
            on_kline_micro_filtered(
                hybrid_micro_sell60_engine,
                symbol,
                candle,
                micro,
                "sell60",
                hybrid_micro_sell60_positions,
                hybrid_micro_sell60_signals,
            )
            on_kline_micro_filtered(
                hybrid_micro_strong_engine,
                symbol,
                candle,
                micro,
                "strong",
                hybrid_micro_strong_positions,
                hybrid_micro_strong_signals,
            )

        tick = Candle(symbol, "agg", ts, price, price, price, price, qty, ts, quote, 1, 0.0 if trade["buyer_maker"] else qty, 0.0 if trade["buyer_maker"] else quote)
        pos = hybrid_full_engine.positions.get(symbol)
        if pos:
            exit_signal = hybrid_full_engine.update_position(pos, tick)
            if exit_signal:
                hybrid_full_signals.append(exit_signal.to_dict())
                hybrid_full_positions.append(pos)
                hybrid_full_engine.positions.pop(symbol, None)

        pos = hybrid_stop_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_stop_engine, pos, tick)
            if exit_signal:
                hybrid_stop_signals.append(exit_signal)
                hybrid_stop_positions.append(pos)
                hybrid_stop_engine.positions.pop(symbol, None)

        pos = hybrid_guard08_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_guard08_engine, pos, tick, early_adverse_pct=0.008)
            if exit_signal:
                hybrid_guard08_signals.append(exit_signal)
                hybrid_guard08_positions.append(pos)
                hybrid_guard08_engine.positions.pop(symbol, None)

        pos = hybrid_guard12_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_guard12_engine, pos, tick, early_adverse_pct=0.012)
            if exit_signal:
                hybrid_guard12_signals.append(exit_signal)
                hybrid_guard12_positions.append(pos)
                hybrid_guard12_engine.positions.pop(symbol, None)

        pos = hybrid_guard16_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_guard16_engine, pos, tick, early_adverse_pct=0.016)
            if exit_signal:
                hybrid_guard16_signals.append(exit_signal)
                hybrid_guard16_positions.append(pos)
                hybrid_guard16_engine.positions.pop(symbol, None)

        pos = hybrid_preclose_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_preclose_engine, pos, tick)
            if exit_signal:
                hybrid_preclose_signals.append(exit_signal)
                hybrid_preclose_positions.append(pos)
                hybrid_preclose_engine.positions.pop(symbol, None)

        pos = hybrid_micro_sell60_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_micro_sell60_engine, pos, tick)
            if exit_signal:
                hybrid_micro_sell60_signals.append(exit_signal)
                hybrid_micro_sell60_positions.append(pos)
                hybrid_micro_sell60_engine.positions.pop(symbol, None)

        pos = hybrid_micro_strong_engine.positions.get(symbol)
        if pos:
            exit_signal = update_position_tick_stop_only(hybrid_micro_strong_engine, pos, tick)
            if exit_signal:
                hybrid_micro_strong_signals.append(exit_signal)
                hybrid_micro_strong_positions.append(pos)
                hybrid_micro_strong_engine.positions.pop(symbol, None)

        pos = agg_engine.positions.get(symbol)
        if pos:
            exit_signal = agg_engine.update_position(pos, tick)
            if exit_signal:
                agg_signals.append(exit_signal.to_dict())
                agg_positions.append(pos)
                agg_engine.positions.pop(symbol, None)
        if ts - last_eval >= int(args["eval_ms"]):
            last_eval = ts
            current = partial_to_candle(symbol, partial, ts)
            agg_engine._append(current)
            if symbol not in agg_engine.positions:
                feat = agg_engine.features(symbol)
                if feat:
                    entry = agg_engine.entry_signal(symbol, feat, current)
                    if entry:
                        pos, signal = entry
                        agg_engine.positions[symbol] = pos
                        agg_signals.append(signal.to_dict())

        if (
            partial is not None
            and ts - last_preclose_eval >= int(args["eval_ms"])
            and ts - int(partial["open_time"]) >= int(args.get("preclose_age_ms", 45_000))
        ):
            last_preclose_eval = ts
            current = partial_to_candle(symbol, partial, ts)
            hybrid_preclose_engine._append(current)
            if symbol not in hybrid_preclose_engine.positions:
                feat = hybrid_preclose_engine.features(symbol)
                if feat:
                    entry = hybrid_preclose_engine.entry_signal(symbol, feat, current)
                    if entry:
                        pos, signal = entry
                        hybrid_preclose_engine.positions[symbol] = pos
                        hybrid_preclose_signals.append(signal.to_dict())
    return (
        agg_positions,
        agg_signals,
        hybrid_full_positions,
        hybrid_full_signals,
        hybrid_stop_positions,
        hybrid_stop_signals,
        hybrid_guard08_positions,
        hybrid_guard08_signals,
        hybrid_guard12_positions,
        hybrid_guard12_signals,
        hybrid_guard16_positions,
        hybrid_guard16_signals,
        hybrid_preclose_positions,
        hybrid_preclose_signals,
        hybrid_micro_sell60_positions,
        hybrid_micro_sell60_signals,
        hybrid_micro_strong_positions,
        hybrid_micro_strong_signals,
        processed,
    )


def on_kline_micro_filtered(
    engine: WaterfallEngine,
    symbol: str,
    candle: Candle,
    micro: dict[str, float],
    filter_name: str,
    positions: list[WaterfallPosition],
    signals: list[dict[str, Any]],
) -> None:
    engine._append(candle)
    pos = engine.positions.get(symbol)
    if pos:
        exit_signal = engine.update_position(pos, candle)
        if pos.status == "closed":
            positions.append(pos)
        if exit_signal:
            signals.append(exit_signal.to_dict())
            engine.positions.pop(symbol, None)
    feat = engine.features(symbol)
    if symbol not in engine.positions and feat and micro_filter_ok(micro, filter_name):
        entry = engine.entry_signal(symbol, feat, candle)
        if entry:
            pos, signal = entry
            engine.positions[symbol] = pos
            signals.append(signal.to_dict())


def micro_filter_ok(micro: dict[str, float], name: str) -> bool:
    if not micro:
        return False
    sell_ratio = float(micro.get("agg_sell_ratio", 0.0))
    low_sec = float(micro.get("agg_low_sec_frac", 0.0))
    if name == "sell60":
        return sell_ratio >= 0.60
    if name == "strong":
        return sell_ratio >= 0.60 and low_sec >= 0.75
    raise ValueError(f"unknown micro filter: {name}")


def micro_from_partial(p: dict[str, Any]) -> dict[str, float]:
    high = float(p["high"])
    low = float(p["low"])
    close = float(p["close"])
    open_ = float(p["open"])
    quote = float(p["quote_volume"])
    last10_quote = float(p.get("last10_quote", 0.0))
    price_50s = float(p.get("price_50s", 0.0) or open_)
    return {
        "agg_trades": float(p["trades"]),
        "agg_quote": quote,
        "agg_sell_ratio": float(p.get("sell_quote", 0.0)) / quote if quote else 0.0,
        "agg_last10_sell_ratio": float(p.get("last10_sell_quote", 0.0)) / last10_quote if last10_quote else 0.0,
        "agg_last10_quote_share": last10_quote / quote if quote else 0.0,
        "agg_ret": close / open_ - 1.0 if open_ else 0.0,
        "agg_last10_ret": close / price_50s - 1.0 if price_50s else 0.0,
        "agg_close_pos": (close - low) / (high - low) if high > low else 0.5,
        "agg_rebound_from_low": close / low - 1.0 if low else 0.0,
        "agg_low_sec_frac": float(p.get("low_sec", 0)) / 59.0,
    }


def update_position_tick_stop_only(
    engine: WaterfallEngine,
    pos: WaterfallPosition,
    tick: Candle,
    early_adverse_pct: float = 0.0,
) -> dict[str, Any] | None:
    profile = engine.profiles[pos.exit_profile]
    now = tick.close_time
    pos.worst_price = max(pos.worst_price, tick.high)
    pos.updated_time = now
    exit_price = 0.0
    reason = ""
    if tick.high >= pos.stop_price:
        exit_price = pos.stop_price
        reason = "stop_loss_tick"
    else:
        age_min = max(0, int((now - pos.entry_time) / MINUTE_MS))
        broken_level = evidence_float(pos.evidence, "broken_level", 0.0)
        if age_min <= 3 and broken_level and tick.close > broken_level * (1.0 + profile.quick_reclaim_buffer):
            exit_price = tick.close
            reason = "stop_quick_reclaim_tick"
        elif early_adverse_pct > 0 and now - pos.entry_time <= 45_000 and tick.close >= pos.entry_price * (1.0 + early_adverse_pct):
            exit_price = tick.close
            reason = f"stop_early_adverse_{early_adverse_pct:.3f}"
    if not reason:
        return None
    pos.status = "closed"
    pos.exit_time = now
    pos.exit_price = exit_price
    pos.exit_reason = reason
    pos.pnl_pct = 1.0 - exit_price / pos.entry_price - pos.fee_rate if exit_price > 0 and pos.entry_price > 0 else 0.0
    pos.pnl_usdt = pos.notional_usdt * pos.pnl_pct
    engine.last_stop_time[pos.symbol] = now
    return {
        "signal_id": f"wf-exit-{pos.symbol}-{now}-{reason}",
        "position_id": pos.position_id,
        "symbol": pos.symbol,
        "strategy": pos.strategy,
        "action": "stop_loss",
        "family": pos.family,
        "rule": pos.rule,
        "decision_time": now,
        "price": exit_price,
        "stop_price": pos.stop_price,
        "pnl_pct": pos.pnl_pct,
        "confidence": 0.0,
        "evidence": [*pos.evidence, f"exit_reason={reason}"],
    }


def load_settings_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def discover_symbols(root: Path, raw: str, max_symbols: int, order: str = "name") -> list[str]:
    if raw:
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        dirs = [p for p in root.iterdir() if p.is_dir() and any(p.glob("*.zip"))]
        if order in {"size_asc", "size_desc"}:
            dirs.sort(key=lambda p: sum(x.stat().st_size for x in p.glob("*.zip")), reverse=(order == "size_desc"))
        else:
            dirs.sort(key=lambda p: p.name.upper())
        symbols = [p.name.upper() for p in dirs]
    return symbols[:max_symbols] if max_symbols > 0 else symbols


def load_klines(symbol: str, klines_dir: Path, start_ms: int, end_ms: int, prewarm: int) -> list[Candle]:
    path = klines_dir / f"{symbol}.parquet"
    if not path.exists():
        return []
    columns = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "num_trades",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    ]
    before: deque[Candle] = deque(maxlen=max(1, prewarm))
    period: list[Candle] = []
    last_ts: int | None = None
    pf = pq.ParquetFile(path)
    stop = False
    for batch in pf.iter_batches(batch_size=65_536, columns=columns):
        data = batch.to_pydict()
        for i, raw_ts in enumerate(data["timestamp"]):
            ts = int(raw_ts)
            if last_ts == ts:
                continue
            last_ts = ts
            candle = Candle(
                symbol=symbol,
                interval="1m",
                open_time=ts,
                open=float(data["open"][i]),
                high=float(data["high"][i]),
                low=float(data["low"][i]),
                close=float(data["close"][i]),
                volume=float(data["volume"][i]),
                close_time=ts + MINUTE_MS - 1,
                quote_volume=float(data["quote_volume"][i]),
                trades=int(batch_value(data, "num_trades", i, 0) or 0),
                taker_buy_base=float(batch_value(data, "taker_buy_base_volume", i, 0.0) or 0.0),
                taker_buy_quote=float(batch_value(data, "taker_buy_quote_volume", i, 0.0) or 0.0),
            )
            if candle.close_time < start_ms:
                before.append(candle)
            elif candle.close_time <= end_ms:
                period.append(candle)
            elif candle.open_time > end_ms:
                stop = True
                break
        if stop:
            break
    return [*before, *period]


def batch_value(data: dict[str, list[Any]], key: str, index: int, default: Any) -> Any:
    values = data.get(key)
    if not values or index >= len(values):
        return default
    return values[index]


def summarize_positions(positions: list[WaterfallPosition], signals: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(positions)
    profit = sum(max(0.0, p.pnl_pct) for p in positions)
    loss = -sum(min(0.0, p.pnl_pct) for p in positions)
    wins = sum(1 for p in positions if p.pnl_pct > 0)
    return {
        "signals": len(signals),
        "trades": n,
        "wins": wins,
        "gross_profit": profit,
        "gross_loss": loss,
        "win_rate": wins / n if n else 0.0,
        "avg_pnl_pct": sum(p.pnl_pct for p in positions) / n if n else 0.0,
        "median_pnl_pct": median([p.pnl_pct for p in positions]),
        "profit_factor": profit / loss if loss > 0 else (99.0 if profit > 0 else 0.0),
        "avg_mae_pct": sum((p.worst_price / p.entry_price - 1.0) for p in positions if p.entry_price > 0) / n if n else 0.0,
        "avg_mfe_pct": sum((p.entry_price / p.best_price - 1.0) for p in positions if p.best_price > 0) / n if n else 0.0,
        "big_3pct": sum(1 for p in positions if p.pnl_pct >= 0.03) / n if n else 0.0,
        "big_5pct": sum(1 for p in positions if p.pnl_pct >= 0.05) / n if n else 0.0,
    }


def summarize_rows(rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    trades = int(sum(int(r[f"{prefix}_trades"]) for r in rows))
    signals = int(sum(int(r[f"{prefix}_signals"]) for r in rows))
    weighted = lambda key: sum(float(r[f"{prefix}_{key}"]) * int(r[f"{prefix}_trades"]) for r in rows) / trades if trades else 0.0
    profit = sum(float(r[f"{prefix}_gross_profit"]) for r in rows)
    loss = sum(float(r[f"{prefix}_gross_loss"]) for r in rows)
    wins = sum(float(r[f"{prefix}_wins"]) for r in rows)
    return {
        "signals": signals,
        "trades": trades,
        "trades_per_day": trades / max(1, days_from_rows(rows)),
        "win_rate": wins / trades if trades else 0.0,
        "avg_pnl_pct": weighted("avg_pnl_pct"),
        "median_symbol_pnl_pct": median([float(r[f"{prefix}_median_pnl_pct"]) for r in rows if int(r[f"{prefix}_trades"]) > 0]),
        "profit_factor": profit / loss if loss > 0 else (99.0 if profit > 0 else 0.0),
        "avg_mae_pct": weighted("avg_mae_pct"),
        "avg_mfe_pct": weighted("avg_mfe_pct"),
        "big_3pct": weighted("big_3pct"),
        "big_5pct": weighted("big_5pct"),
    }


def days_from_rows(rows: list[dict[str, Any]]) -> int:
    return int(rows[0].get("_days", 4)) if rows else 1


def prefix_metrics(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{k}": v for k, v in metrics.items()}


def median(values: list[float]) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def write_dicts(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
