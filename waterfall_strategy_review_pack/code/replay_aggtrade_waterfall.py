"""Replay waterfall strategy with Binance Vision aggTrade files.

This simulates faster-than-1m entry/exit:
- historical closed 1m klines provide context before the replay window;
- aggTrade rows build the current not-yet-closed 1m candle;
- entry rules can trigger on the partial 1m candle;
- exits are evaluated on every aggregate trade price.
"""
from __future__ import annotations

import argparse
import csv
import json
import zipfile
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq

from pump_dump_hunter.models import Candle
from pump_dump_hunter.waterfall import WaterfallEngine, WaterfallPosition


MINUTE_MS = 60_000


def main() -> int:
    args = parse_args()
    settings = load_settings_json(args.config)
    if args.variant:
        settings.setdefault("waterfall_quant", {})["variant"] = args.variant
    if args.families:
        settings.setdefault("waterfall_quant", {})["enabled_families"] = [
            x.strip() for x in args.families.split(",") if x.strip()
        ]
    engine = WaterfallEngine(settings)
    symbols = discover_symbols(Path(args.agg_dir), args.symbols, args.max_symbols)
    start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int((datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1
    all_signals = []
    closed: list[WaterfallPosition] = []
    processed = 0

    for idx, symbol in enumerate(symbols, 1):
        prime_context(engine, symbol, Path(args.klines_dir), start_ms, int(args.prewarm))
        partial: dict[str, Any] | None = None
        last_eval = 0
        for trade in iter_symbol_trades(Path(args.agg_dir), symbol, date.fromisoformat(args.start), date.fromisoformat(args.end)):
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
                    engine._append(partial_to_candle(symbol, partial, partial["open_time"] + MINUTE_MS - 1))
                partial = {
                    "open_time": minute,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": qty,
                    "quote_volume": quote,
                    "trades": 1,
                    "taker_buy_quote": 0.0 if trade["buyer_maker"] else quote,
                    "taker_buy_base": 0.0 if trade["buyer_maker"] else qty,
                }
            else:
                partial["high"] = max(float(partial["high"]), price)
                partial["low"] = min(float(partial["low"]), price)
                partial["close"] = price
                partial["volume"] += qty
                partial["quote_volume"] += quote
                partial["trades"] += 1
                if not trade["buyer_maker"]:
                    partial["taker_buy_quote"] += quote
                    partial["taker_buy_base"] += qty

            tick = Candle(symbol, "agg", ts, price, price, price, price, qty, ts, quote, 1, 0.0 if trade["buyer_maker"] else qty, 0.0 if trade["buyer_maker"] else quote)
            pos = engine.positions.get(symbol)
            if pos:
                exit_signal = engine.update_position(pos, tick)
                if exit_signal:
                    all_signals.append(exit_signal.to_dict())
                    closed.append(pos)
                    engine.positions.pop(symbol, None)

            if ts - last_eval >= int(args.eval_ms):
                last_eval = ts
                current = partial_to_candle(symbol, partial, ts)
                engine._append(current)
                if symbol not in engine.positions:
                    feat = engine.features(symbol)
                    if feat:
                        entry = engine.entry_signal(symbol, feat, current)
                        if entry:
                            pos, signal = entry
                            engine.positions[symbol] = pos
                            all_signals.append(signal.to_dict())

        if partial is not None:
            engine._append(partial_to_candle(symbol, partial, int(partial["open_time"]) + MINUTE_MS - 1))
        if idx % max(1, args.progress_every) == 0:
            print(f"replayed {idx}/{len(symbols)} trades={processed} signals={len(all_signals)} closed={len(closed)}", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    signals_path = out_dir / f"agg_waterfall_signals_{stamp}.csv"
    positions_path = out_dir / f"agg_waterfall_positions_{stamp}.csv"
    write_dicts(signals_path, all_signals)
    write_dicts(positions_path, [position_row(p) for p in closed])
    metrics = summarize(closed, all_signals, processed, symbols, args)
    metrics["signals_path"] = str(signals_path)
    metrics["positions_path"] = str(positions_path)
    metrics_path = out_dir / f"agg_waterfall_metrics_{stamp}.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="backend/config/settings.json")
    p.add_argument("--agg-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--klines-dir", default=r"E:\A\bb\data\klines")
    p.add_argument("--out-dir", default="backend/storage/ml/agg_waterfall_replay")
    p.add_argument("--symbols", default="")
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--variant", choices=["core", "high_pf"], default="")
    p.add_argument("--families", default="")
    p.add_argument("--prewarm", type=int, default=1500)
    p.add_argument("--eval-ms", type=int, default=1000)
    p.add_argument("--progress-every", type=int, default=10)
    return p.parse_args()


def load_settings_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def discover_symbols(root: Path, raw: str, max_symbols: int) -> list[str]:
    if raw:
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        symbols = sorted(p.name.upper() for p in root.iterdir() if p.is_dir())
    return symbols[:max_symbols] if max_symbols > 0 else symbols


def prime_context(engine: WaterfallEngine, symbol: str, klines_dir: Path, start_ms: int, prewarm: int) -> None:
    path = klines_dir / f"{symbol}.parquet"
    if not path.exists():
        return
    table = pq.read_table(path)
    df = table.to_pandas()
    df = df[df["timestamp"] < start_ms].tail(prewarm)
    candles = []
    for row in df.itertuples(index=False):
        ts = int(row.timestamp)
        candles.append(
            Candle(
                symbol=symbol,
                interval="1m",
                open_time=ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                close_time=ts + MINUTE_MS - 1,
                quote_volume=float(row.quote_volume),
                trades=int(getattr(row, "num_trades", 0) or 0),
                taker_buy_base=float(getattr(row, "taker_buy_base_volume", 0.0) or 0.0),
                taker_buy_quote=float(getattr(row, "taker_buy_quote_volume", 0.0) or 0.0),
            )
        )
    engine.prime_candles(candles)


def iter_symbol_trades(root: Path, symbol: str, start: date, end: date) -> Iterable[dict[str, Any]]:
    d = start
    while d <= end:
        path = root / symbol / f"{symbol}-aggTrades-{d.isoformat()}.zip"
        if path.exists() and path.stat().st_size > 0:
            yield from iter_zip_trades(path)
        d += timedelta(days=1)


def iter_zip_trades(path: Path) -> Iterable[dict[str, Any]]:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return
        with zf.open(names[0]) as fh:
            text = (line.decode("utf-8").strip() for line in fh)
            reader = csv.reader(text)
            for row in reader:
                if not row or not row[0] or not row[0][0].isdigit():
                    continue
                if len(row) < 7:
                    continue
                yield {
                    "id": int(row[0]),
                    "price": float(row[1]),
                    "qty": float(row[2]),
                    "time": int(row[5]),
                    "buyer_maker": str(row[6]).strip().lower() == "true",
                }


def partial_to_candle(symbol: str, p: dict[str, Any], close_time: int) -> Candle:
    return Candle(
        symbol=symbol,
        interval="1m",
        open_time=int(p["open_time"]),
        open=float(p["open"]),
        high=float(p["high"]),
        low=float(p["low"]),
        close=float(p["close"]),
        volume=float(p["volume"]),
        close_time=int(close_time),
        quote_volume=float(p["quote_volume"]),
        trades=int(p["trades"]),
        taker_buy_base=float(p["taker_buy_base"]),
        taker_buy_quote=float(p["taker_buy_quote"]),
    )


def position_row(p: WaterfallPosition) -> dict[str, Any]:
    row = p.to_dict()
    row["entry_iso"] = iso_ms(p.entry_time)
    row["exit_iso"] = iso_ms(p.exit_time or 0)
    return row


def summarize(closed: list[WaterfallPosition], signals: list[dict[str, Any]], processed: int, symbols: list[str], args: argparse.Namespace) -> dict[str, Any]:
    profit = sum(max(0.0, p.pnl_pct) for p in closed)
    loss = -sum(min(0.0, p.pnl_pct) for p in closed)
    wins = sum(1 for p in closed if p.pnl_pct > 0)
    days = (date.fromisoformat(args.end) - date.fromisoformat(args.start)).days + 1
    avg = sum(p.pnl_pct for p in closed) / len(closed) if closed else 0.0
    med = median([p.pnl_pct for p in closed])
    avg_mae = sum((p.worst_price / p.entry_price - 1.0) for p in closed if p.entry_price > 0) / len(closed) if closed else 0.0
    avg_mfe = sum((p.entry_price / p.best_price - 1.0) for p in closed if p.best_price > 0) / len(closed) if closed else 0.0
    return {
        "mode": "aggTrade_partial_1m_replay",
        "variant": args.variant or "core",
        "symbols": len(symbols),
        "start": args.start,
        "end": args.end,
        "days": days,
        "agg_trades_processed": processed,
        "signals": len(signals),
        "closed_trades": len(closed),
        "trades_per_day": len(closed) / max(1, days),
        "win_rate": wins / len(closed) if closed else 0.0,
        "avg_pnl_pct": avg,
        "median_pnl_pct": med,
        "profit_factor": profit / loss if loss > 0 else None,
        "avg_mae_pct": avg_mae,
        "avg_mfe_pct": avg_mfe,
        "big_3pct_rate": sum(1 for p in closed if p.pnl_pct >= 0.03) / len(closed) if closed else 0.0,
        "big_5pct_rate": sum(1 for p in closed if p.pnl_pct >= 0.05) / len(closed) if closed else 0.0,
        "by_family": summarize_group(closed, "family"),
        "by_exit_reason": summarize_group(closed, "exit_reason"),
    }


def summarize_group(positions: list[WaterfallPosition], attr: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in positions:
        key = str(getattr(p, attr))
        row = out.setdefault(key, {"trades": 0, "wins": 0, "pnl_sum": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if p.pnl_pct > 0 else 0
        row["pnl_sum"] += p.pnl_pct
    for row in out.values():
        row["win_rate"] = row["wins"] / row["trades"] if row["trades"] else 0.0
        row["avg_pnl_pct"] = row["pnl_sum"] / row["trades"] if row["trades"] else 0.0
    return out


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def write_dicts(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def iso_ms(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
