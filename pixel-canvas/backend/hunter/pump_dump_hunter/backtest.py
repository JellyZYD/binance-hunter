from __future__ import annotations

import itertools
import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import compute_liquidity_records
from .engine.signal_engine import SignalEngine
from .models import Alert, Candle, KlineClosed, PumpEvent, SignalParams
from .timeutils import interval_to_ms


@dataclass
class BacktestResult:
    params: SignalParams
    alerts: list[Alert]
    metrics: dict[str, Any]


def run_backtest_from_store(
    store: Any,
    settings: dict[str, Any],
    params: SignalParams,
    days: int,
    top_n: int,
) -> BacktestResult:
    symbols = [s for s in store.candle_symbols("1m") if s not in backtest_exclude_symbols(settings)]
    if not symbols:
        return BacktestResult(params=params, alerts=[], metrics={"error": "no historical 1m candles"})
    end_time = store.max_candle_close_time("1m", symbols)
    if not end_time:
        return BacktestResult(params=params, alerts=[], metrics={"error": "no historical 1m candles"})
    start_time = end_time - int(days) * 86_400_000
    load_start_time = start_time - 2 * 86_400_000
    one_by_symbol = {s: store.load_candles(s, "1m", start_time=load_start_time, end_time=end_time) for s in symbols}
    fifteen_by_symbol = {s: ensure_interval(store, s, "15m", load_start_time, end_time, one_by_symbol[s]) for s in symbols}
    close_times_by_symbol = {s: [c.close_time for c in rows] for s, rows in one_by_symbol.items()}
    fifteen_close_times_by_symbol = {s: [c.close_time for c in rows] for s, rows in fifteen_by_symbol.items()}
    engine = SignalEngine(settings, params=params)
    alerts: list[Alert] = []
    discovery_every = 15 * 60_000
    primed_event_ids: set[str] = set()

    cutoff = ((start_time // discovery_every) + 1) * discovery_every - 1
    prewarm_limit = int(settings["websocket"].get("prewarm_limit", 80))
    while cutoff <= end_time:
        records = records_from_historical(
            one_by_symbol,
            fifteen_by_symbol,
            cutoff,
            top_n,
            params,
            close_times_by_symbol,
            fifteen_close_times_by_symbol,
        )
        engine.on_discovery(records, cutoff)
        events_to_prime = active_backtest_events(engine, cutoff, exclude_event_ids=primed_event_ids)
        prime_active_backtest_buffers(
            engine,
            one_by_symbol,
            fifteen_by_symbol,
            close_times_by_symbol,
            fifteen_close_times_by_symbol,
            cutoff,
            prewarm_limit,
            events_to_prime,
        )
        primed_event_ids.update(event.event_id for event in events_to_prime)
        next_cutoff = min(cutoff + discovery_every, end_time)
        active = active_event_symbols(engine, cutoff)
        if active:
            events = []
            for symbol in active:
                for candle in slice_candles(fifteen_by_symbol.get(symbol, []), fifteen_close_times_by_symbol.get(symbol, []), cutoff, next_cutoff):
                    events.append(KlineClosed(candle.symbol, "15m", candle))
            events.sort(key=lambda e: (e.candle.close_time, e.candle.interval, e.symbol))
            for event in events:
                _changed, new_alerts = engine.on_kline(event)
                alerts.extend(new_alerts)
        cutoff += discovery_every
    metrics = evaluate_alerts(alerts, one_by_symbol, settings)
    metrics["period_start"] = start_time
    metrics["period_end"] = end_time
    return BacktestResult(params=params, alerts=alerts, metrics=metrics)


def backtest_exclude_symbols(settings: dict[str, Any]) -> set[str]:
    out = {str(s).upper() for s in settings.get("universe", {}).get("exclude_symbols", [])}
    out |= {str(s).upper() for s in settings.get("bb_import", {}).get("exclude_symbols", [])}
    return out


def records_from_historical(
    one_by_symbol: dict[str, list[Candle]],
    fifteen_by_symbol: dict[str, list[Candle]],
    cutoff: int,
    top_n: int,
    params: SignalParams,
    close_times_by_symbol: dict[str, list[int]] | None = None,
    fifteen_close_times_by_symbol: dict[str, list[int]] | None = None,
) -> list[Any]:
    broad = []
    candles_by_symbol = {}
    context_by_symbol = {}
    for symbol, rows in one_by_symbol.items():
        if close_times_by_symbol is not None:
            idx = bisect_right(close_times_by_symbol.get(symbol, []), cutoff)
            if idx < 61:
                continue
            closed = rows[max(0, idx - 61) : idx]
        else:
            closed = [c for c in rows if c.close_time <= cutoff]
        if len(closed) < 61:
            continue
        candles_by_symbol[symbol] = closed[-61:]
        context_rows = fifteen_by_symbol.get(symbol, [])
        if fifteen_close_times_by_symbol is not None:
            context_idx = bisect_right(fifteen_close_times_by_symbol.get(symbol, []), cutoff)
            context_closed = context_rows[max(0, context_idx - 97) : context_idx]
        else:
            context_closed = [c for c in context_rows if c.close_time <= cutoff][-97:]
        context_by_symbol[symbol] = context_closed
        broad.append({"symbol": symbol, "last_price": closed[-1].close, "pct_24h": 0.0})
    return compute_liquidity_records(
        broad,
        candles_by_symbol,
        top_n=top_n,
        data_cutoff_time=cutoff,
        params=params,
        context_candles_by_symbol=context_by_symbol,
    )


def active_event_symbols(engine: SignalEngine, now_ms: int) -> list[str]:
    return sorted({event.symbol for event in active_backtest_events(engine, now_ms)})


def active_backtest_events(
    engine: SignalEngine,
    now_ms: int,
    exclude_event_ids: set[str] | None = None,
) -> list[PumpEvent]:
    excluded = exclude_event_ids or set()
    events = []
    for event in engine.events_by_symbol.values():
        if (
            event.status == "active"
            and event.expires_at >= now_ms
            and event.event_id not in excluded
            and event.symbol.isascii()
            and event.symbol.isalnum()
        ):
            events.append(event)
    return sorted(events, key=lambda e: (e.symbol, e.event_id))


def slice_candles(rows: list[Candle], close_times: list[int], start_exclusive: int, end_inclusive: int) -> list[Candle]:
    if not rows or not close_times or end_inclusive <= start_exclusive:
        return []
    left = bisect_right(close_times, start_exclusive)
    right = bisect_right(close_times, end_inclusive)
    return rows[left:right]


def prime_active_backtest_buffers(
    engine: SignalEngine,
    one_by_symbol: dict[str, list[Candle]],
    fifteen_by_symbol: dict[str, list[Candle]],
    close_times_by_symbol: dict[str, list[int]],
    fifteen_close_times_by_symbol: dict[str, list[int]],
    cutoff: int,
    limit: int,
    events: list[PumpEvent] | None = None,
) -> None:
    warmup: list[Candle] = []
    symbols = [event.symbol for event in events] if events is not None else active_event_symbols(engine, cutoff)
    for symbol in symbols:
        fifteen_rows = fifteen_by_symbol.get(symbol, [])
        fifteen_times = fifteen_close_times_by_symbol.get(symbol, [])
        fifteen_right = bisect_right(fifteen_times, cutoff)
        warmup.extend(fifteen_rows[max(0, fifteen_right - limit) : fifteen_right])
    if warmup:
        engine.prime_candles(warmup)


def ensure_interval(store: Any, symbol: str, interval: str, start_time: int, end_time: int, one_minute: list[Candle]) -> list[Candle]:
    stored = store.load_candles(symbol, interval, start_time=start_time, end_time=end_time)
    if stored:
        return stored
    return aggregate_1m_to_interval(one_minute, interval)


def aggregate_1m_to_5m(rows: list[Candle]) -> list[Candle]:
    return aggregate_1m_to_interval(rows, "5m")


def aggregate_1m_to_interval(rows: list[Candle], interval: str) -> list[Candle]:
    out = []
    bucket_ms = interval_to_ms(interval)
    expected = bucket_ms // interval_to_ms("1m")
    buckets: dict[int, list[Candle]] = {}
    for candle in rows:
        start = (candle.open_time // bucket_ms) * bucket_ms
        buckets.setdefault(start, []).append(candle)
    for start, candles in sorted(buckets.items()):
        if len(candles) < expected:
            continue
        candles = sorted(candles, key=lambda c: c.open_time)
        out.append(
            Candle(
                symbol=candles[0].symbol,
                interval=interval,
                open_time=start,
                close_time=start + bucket_ms - 1,
                open=candles[0].open,
                high=max(c.high for c in candles),
                low=min(c.low for c in candles),
                close=candles[-1].close,
                volume=sum(c.volume for c in candles),
                quote_volume=sum(c.quote_volume for c in candles),
                trades=sum(c.trades for c in candles),
                taker_buy_base=sum(c.taker_buy_base for c in candles),
                taker_buy_quote=sum(c.taker_buy_quote for c in candles),
            )
        )
    return out


def evaluate_alerts(alerts: list[Alert], one_by_symbol: dict[str, list[Candle]], settings: dict[str, Any]) -> dict[str, Any]:
    horizons = [int(h) for h in settings["backtest"]["horizons_minutes"]]
    max_horizon = max(horizons)
    thresholds = [float(v) for v in settings["backtest"].get("profit_thresholds_pct", [3, 5, 10])]
    rows = []
    for alert in alerts:
        future = [c for c in one_by_symbol.get(alert.symbol, []) if c.close_time > alert.decision_time]
        item: dict[str, Any] = {
            "alert_id": alert.alert_id,
            "symbol": alert.symbol,
            "level": alert.level,
            "decision_time": alert.decision_time,
        }
        for horizon in horizons:
            target = alert.decision_time + horizon * 60_000
            closes = [c for c in future if c.close_time >= target]
            if closes:
                item[f"ret_{horizon}m"] = alert.price / closes[0].close - 1.0
        horizon_rows = [c for c in future if c.close_time <= alert.decision_time + max_horizon * 60_000]
        if horizon_rows:
            best = min(horizon_rows, key=lambda c: c.low)
            worst = max(horizon_rows, key=lambda c: c.high)
            max_favorable = alert.price / best.low - 1.0
            max_adverse = worst.high / alert.price - 1.0
            item["max_window_minutes"] = max_horizon
            item["max_favorable"] = max_favorable
            item[f"max_favorable_{max_horizon}m"] = max_favorable
            item["max_adverse"] = max_adverse
            item[f"max_adverse_{max_horizon}m"] = max_adverse
            item["time_to_best_m"] = minutes_after(alert.decision_time, best.close_time)
            add_threshold_metrics(item, alert, horizon_rows, thresholds)
            add_anchor_metrics(item, alert, best.low)
        rows.append(item)
    signal_rows = [r for r in rows if r["level"] == "short_signal"]
    base = summarize_rows(signal_rows)
    base["alerts"] = len(alerts)
    base["rows"] = rows
    min_signals = int(settings["backtest"].get("min_signals", 5))
    if base["short_signals"] < min_signals:
        base["low_sample"] = True
        base["sample_warning"] = f"short_signals {base['short_signals']} < min_signals {min_signals}"
    return base


def add_threshold_metrics(item: dict[str, Any], alert: Alert, horizon_rows: list[Candle], thresholds: list[float]) -> None:
    first_hits: dict[float, Candle] = {}
    for threshold in thresholds:
        target_low = alert.price / (1.0 + threshold / 100.0)
        hit = next((c for c in horizon_rows if c.low <= target_low), None)
        if hit:
            first_hits[threshold] = hit
            item[f"time_to_{threshold_key(threshold)}pct_m"] = minutes_after(alert.decision_time, hit.close_time)

    hit5 = first_hits.get(5.0) or first_hits.get(5)
    if hit5:
        before_hit = [c for c in horizon_rows if c.close_time < hit5.close_time]
    else:
        before_hit = horizon_rows
    if before_hit:
        item["max_adverse_before_5pct"] = max(c.high for c in before_hit) / alert.price - 1.0


def add_anchor_metrics(item: dict[str, Any], alert: Alert, best_low: float) -> None:
    if alert.anchor_price <= 0 or alert.price <= alert.anchor_price:
        return
    remaining_to_anchor = alert.price / alert.anchor_price - 1.0
    captured_to_anchor = alert.price / max(best_low, alert.anchor_price) - 1.0
    item["entry_to_anchor_ret"] = remaining_to_anchor
    item["capture_to_anchor_ratio"] = min(1.0, captured_to_anchor / remaining_to_anchor) if remaining_to_anchor > 0 else 0.0
    item["reached_anchor"] = best_low <= alert.anchor_price


def minutes_after(start_ms: int, end_ms: int) -> int:
    return max(1, int((end_ms - start_ms + 1) // 60_000))


def threshold_key(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", "p")


def summarize_rows(signal_rows: list[dict[str, Any]]) -> dict[str, Any]:
    horizons = sorted(
        {
            int(key[4:-1])
            for row in signal_rows
            for key in row
            if key.startswith("ret_") and key.endswith("m") and key[4:-1].isdigit()
        }
    ) or [5, 15, 30, 60]
    out: dict[str, Any] = {"short_signals": len(signal_rows)}
    for horizon in horizons:
        key = f"ret_{horizon}m"
        vals = [float(r[key]) for r in signal_rows if key in r]
        wins = [v for v in vals if v > 0]
        out[f"win_rate_{horizon}m"] = len(wins) / len(vals) if vals else 0.0
        out[f"avg_ret_{horizon}m"] = sum(vals) / len(vals) if vals else 0.0
    favorable = [float(r["max_favorable"]) for r in signal_rows if "max_favorable" in r]
    adverse = [float(r["max_adverse"]) for r in signal_rows if "max_adverse" in r]
    out["avg_max_favorable"] = avg(favorable)
    out["avg_max_adverse"] = avg(adverse)
    out["fish_body_ratio"] = len([v for v in favorable if v >= 0.03]) / len(favorable) if favorable else 0.0
    out["fish_body_ratio_5pct"] = len([v for v in favorable if v >= 0.05]) / len(favorable) if favorable else 0.0
    out["fish_body_ratio_10pct"] = len([v for v in favorable if v >= 0.10]) / len(favorable) if favorable else 0.0
    out["avg_time_to_best_m"] = avg([float(r["time_to_best_m"]) for r in signal_rows if "time_to_best_m" in r])
    out["avg_max_adverse_before_5pct"] = avg([float(r["max_adverse_before_5pct"]) for r in signal_rows if "max_adverse_before_5pct" in r])
    for threshold in sorted(thresholds_from_rows(signal_rows)):
        key = threshold_key(threshold)
        time_key = f"time_to_{key}pct_m"
        hit_times = [float(r[time_key]) for r in signal_rows if time_key in r]
        out[f"hit_rate_{key}pct"] = len(hit_times) / len(signal_rows) if signal_rows else 0.0
        out[f"avg_time_to_{key}pct_m"] = avg(hit_times)
    capture = [float(r["capture_to_anchor_ratio"]) for r in signal_rows if "capture_to_anchor_ratio" in r]
    reached = [bool(r["reached_anchor"]) for r in signal_rows if "reached_anchor" in r]
    out["avg_capture_to_anchor_ratio"] = avg(capture)
    out["reached_anchor_rate"] = len([v for v in reached if v]) / len(reached) if reached else 0.0
    return out


def thresholds_from_rows(rows: list[dict[str, Any]]) -> set[float]:
    out: set[float] = set()
    prefix = "time_to_"
    suffix = "pct_m"
    for row in rows:
        for key in row:
            if key.startswith(prefix) and key.endswith(suffix):
                raw = key[len(prefix) : -len(suffix)].replace("p", ".")
                try:
                    out.add(float(raw))
                except ValueError:
                    pass
    return out


def optimize_from_store(
    store: Any,
    settings: dict[str, Any],
    grid_path: str | Path,
    days: int,
    top_n: int,
) -> list[dict[str, Any]]:
    grid = json.loads(Path(grid_path).read_text(encoding="utf-8"))
    keys = list(grid.keys())
    results = []
    for values in itertools.product(*(grid[k] for k in keys)):
        overrides = dict(zip(keys, values))
        params = SignalParams.from_dict({**settings["params"], **overrides})
        result = run_backtest_from_store(store, settings, params, days=days, top_n=top_n)
        metrics = result.metrics
        split = split_train_validation(metrics.get("rows", []), metrics["period_start"], metrics["period_end"], float(settings["backtest"]["train_ratio"]))
        metrics["train"] = split["train"]
        metrics["validation"] = split["validation"]
        train_signals = max(1, split["train"].get("short_signals", 0))
        score = (
            split["train"].get("avg_max_favorable", 0.0)
            - 0.6 * split["train"].get("avg_max_adverse", 0.0)
        ) * train_signals
        if split["train"].get("short_signals", 0) < int(settings["backtest"]["min_signals"]):
            score -= 999
            metrics["low_sample"] = True
        results.append({"score": score, "params": params.to_dict(), "metrics": metrics})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def split_train_validation(rows: list[dict[str, Any]], start: int, end: int, train_ratio: float) -> dict[str, Any]:
    split_time = int(start + (end - start) * train_ratio)
    short_rows = [r for r in rows if r.get("level") == "short_signal"]
    train_rows = [r for r in short_rows if int(r["decision_time"]) <= split_time]
    val_rows = [r for r in short_rows if int(r["decision_time"]) > split_time]
    train = summarize_rows(train_rows)
    validation = summarize_rows(val_rows)
    train["start"] = start
    train["end"] = split_time
    validation["start"] = split_time + 1
    validation["end"] = end
    return {"train": train, "validation": validation}
