"""Replay the production BoardWaterfallEngine over local 1m parquet data.

Candles from every symbol are merged by close time and sent through one engine,
so account equity, margin use, position limits, cooldowns, fees and slippage
match the live paper implementation. Open positions at the replay boundary are
reported separately and are not force-closed into performance metrics.
"""
from __future__ import annotations

import argparse
import csv
import heapq
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pump_dump_hunter.board_waterfall import BoardWaterfallEngine
from pump_dump_hunter.models import Candle, KlineClosed


MINUTE_MS = 60_000
COLUMNS = [
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


def main() -> int:
    args = parse_args()
    settings = json.loads(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.klines_dir)
    symbols = discover_symbols(root, args.symbols, args.max_symbols, settings)
    start_ms = day_start_ms(args.start)
    end_ms = day_start_ms(args.end) + 86_400_000 - 1
    warm_start = start_ms - int(args.prewarm_days) * 86_400_000
    split_ms = day_start_ms(args.split_date) if args.split_date else 0

    engine = BoardWaterfallEngine(settings)
    closed: list[dict[str, Any]] = []
    signal_count = 0
    candle_count = 0
    for candle in merged_candles(root, symbols, warm_start, end_ms):
        if candle.close_time < start_ms:
            engine.prime_candles([candle])
            continue
        candle_count += 1
        _watch, changed, signals = engine.on_kline(KlineClosed(candle.symbol, "1m", candle))
        signal_count += len(signals)
        for position in changed:
            if position.status == "closed":
                closed.append(position.to_dict())
        if args.progress_every > 0 and candle_count % args.progress_every == 0:
            print(f"replayed candles={candle_count:,} closed={len(closed):,} open={len(engine.positions)}", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trades_path = out_dir / f"board_waterfall_trades_{stamp}.csv"
    write_csv(trades_path, closed)
    days = (date.fromisoformat(args.end) - date.fromisoformat(args.start)).days + 1
    report: dict[str, Any] = {
        "strategy": engine.strategy,
        "config": str(Path(args.config).resolve()),
        "klines_dir": str(root.resolve()),
        "start": args.start,
        "end": args.end,
        "split_date": args.split_date,
        "symbols": len(symbols),
        "candles": candle_count,
        "signals": signal_count,
        "open_at_end": len(engine.positions),
        "all": metrics(closed, days),
        "trades_csv": str(trades_path.resolve()),
    }
    if split_ms:
        train_days = max(1, (date.fromisoformat(args.split_date) - date.fromisoformat(args.start)).days)
        holdout_days = max(1, days - train_days)
        report["train"] = metrics([row for row in closed if int(row["entry_time"]) < split_ms], train_days)
        report["holdout"] = metrics([row for row in closed if int(row["entry_time"]) >= split_ms], holdout_days)

    json_path = out_dir / f"board_waterfall_report_{stamp}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="backend/config/settings.json")
    parser.add_argument("--klines-dir", default=r"E:\A\bb\data\klines")
    parser.add_argument("--out-dir", default="backend/storage/backtests/board_waterfall")
    parser.add_argument("--start", required=True, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--split-date", default="", help="Optional holdout start date")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols; default is all parquet files")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--prewarm-days", type=int, default=2)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    return parser.parse_args()


def discover_symbols(root: Path, raw: str, max_symbols: int, settings: dict[str, Any]) -> list[str]:
    excluded = {str(symbol).upper() for symbol in settings.get("universe", {}).get("exclude_symbols", [])}
    if raw:
        symbols = [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]
    else:
        symbols = sorted(path.stem.upper() for path in root.glob("*.parquet") if not path.name.endswith(".bak"))
    symbols = [symbol for symbol in symbols if symbol.endswith("USDT") and symbol not in excluded]
    return symbols[:max_symbols] if max_symbols > 0 else symbols


def merged_candles(root: Path, symbols: list[str], start_ms: int, end_ms: int) -> Iterator[Candle]:
    heap: list[tuple[int, int, Candle, Iterator[Candle]]] = []
    for sequence, symbol in enumerate(symbols):
        iterator = iter_symbol_candles(root / f"{symbol}.parquet", symbol, start_ms, end_ms)
        first = next(iterator, None)
        if first is not None:
            heapq.heappush(heap, (first.close_time, sequence, first, iterator))
    while heap:
        _close_time, sequence, candle, iterator = heapq.heappop(heap)
        yield candle
        nxt = next(iterator, None)
        if nxt is not None:
            heapq.heappush(heap, (nxt.close_time, sequence, nxt, iterator))


def iter_symbol_candles(path: Path, symbol: str, start_ms: int, end_ms: int) -> Iterator[Candle]:
    if not path.exists():
        return
    parquet = pq.ParquetFile(path)
    last_timestamp = -1
    for batch in parquet.iter_batches(batch_size=65_536, columns=COLUMNS):
        data = batch.to_pydict()
        for index, raw_timestamp in enumerate(data["timestamp"]):
            timestamp = int(raw_timestamp)
            if timestamp == last_timestamp or timestamp < start_ms:
                continue
            if timestamp > end_ms:
                return
            last_timestamp = timestamp
            yield Candle(
                symbol=symbol,
                interval="1m",
                open_time=timestamp,
                open=float(data["open"][index]),
                high=float(data["high"][index]),
                low=float(data["low"][index]),
                close=float(data["close"][index]),
                volume=float(data["volume"][index]),
                close_time=timestamp + MINUTE_MS - 1,
                quote_volume=float(data["quote_volume"][index]),
                trades=int(value(data, "num_trades", index, 0)),
                taker_buy_base=float(value(data, "taker_buy_base_volume", index, 0.0)),
                taker_buy_quote=float(value(data, "taker_buy_quote_volume", index, 0.0)),
            )


def metrics(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    pnl = [float(row.get("pnl_pct") or 0.0) for row in rows]
    gross_profit = sum(max(0.0, item) for item in pnl)
    gross_loss = -sum(min(0.0, item) for item in pnl)
    biggest = max(pnl, default=0.0)
    pnl_without_biggest = list(pnl)
    if pnl_without_biggest:
        pnl_without_biggest.remove(biggest)
    return {
        "trades": len(rows),
        "trades_per_day": len(rows) / max(1, days),
        "win_rate": sum(item > 0 for item in pnl) / len(pnl) if pnl else 0.0,
        "avg_pnl_pct": mean(pnl),
        "median_pnl_pct": median(pnl),
        "profit_factor": profit_factor(pnl),
        "avg_mae_pct": mean([
            max(0.0, float(row.get("worst_price") or 0.0) / float(row.get("entry_price") or 1.0) - 1.0)
            for row in rows
        ]),
        "avg_mfe_pct": mean([
            max(0.0, float(row.get("entry_price") or 0.0) / float(row.get("best_price") or 1.0) - 1.0)
            for row in rows
        ]),
        "big_3pct": sum(item >= 0.03 for item in pnl) / len(pnl) if pnl else 0.0,
        "big_5pct": sum(item >= 0.05 for item in pnl) / len(pnl) if pnl else 0.0,
        "largest_trade_pct": biggest,
        "profit_factor_without_largest_trade": profit_factor(pnl_without_biggest),
        "avg_pnl_without_largest_trade_pct": mean(pnl_without_biggest),
        "gross_profit_pct": gross_profit,
        "gross_loss_pct": gross_loss,
    }


def profit_factor(values: list[float]) -> float:
    profit = sum(max(0.0, item) for item in values)
    loss = -sum(min(0.0, item) for item in values)
    return profit / loss if loss > 0 else (99.0 if profit > 0 else 0.0)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0


def day_start_ms(raw: str) -> int:
    return int(datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp() * 1000)


def value(data: dict[str, list[Any]], key: str, index: int, default: Any) -> Any:
    values = data.get(key)
    if not values or index >= len(values) or values[index] is None:
        return default
    return values[index]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
