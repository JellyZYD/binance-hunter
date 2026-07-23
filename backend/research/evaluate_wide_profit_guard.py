"""Evaluate an exchange-hosted wide trailing profit guard.

The production strategy remains the source of entries and normal exits. This
script asks one narrow question: would a second, wider Binance-style trailing
guard protect profits during a fast rebound without cutting too much of the
existing edge?

The 1m study reports two intrabar bounds:

* conservative: a new low cannot trigger a rebound exit in the same minute;
* low_first_upper: the minute low is assumed to occur before its high.

Only the conservative result may be used to select a production candidate.
For candidate trades with downloaded Binance Vision aggTrades, the script also
replays every trade in timestamp order and reports the exact observed trigger.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pyarrow.parquet as pq


MINUTE_MS = 60_000
FEE_RATE = 0.0008
EXIT_SLIPPAGE = 0.001
DEFAULT_ACTIVATIONS = (0.05, 0.055, 0.06, 0.07, 0.08)
DEFAULT_CALLBACKS = (0.04, 0.045, 0.05)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", required=True)
    parser.add_argument("--klines-dir", required=True)
    parser.add_argument("--agg-dir", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--split-date", default="2026-01-01")
    parser.add_argument("--agg-max-events", type=int, default=250)
    parser.add_argument("--activations", default=",".join(map(str, DEFAULT_ACTIVATIONS)))
    parser.add_argument("--callbacks", default=",".join(map(str, DEFAULT_CALLBACKS)))
    return parser.parse_args()


def read_trades(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in (
            "entry_price", "entry_time", "exit_price", "exit_time", "pnl_pct",
            "stop_price", "trail_price", "fee_rate",
        ):
            row[key] = float(row.get(key) or 0)
        row["entry_time"] = int(row["entry_time"])
        row["exit_time"] = int(row["exit_time"])
    return rows


def load_symbol_bars(
    path: Path,
    start_ms: int,
    end_ms: int,
) -> dict[str, np.ndarray]:
    table = pq.read_table(
        path,
        columns=["timestamp", "open", "high", "low", "close"],
        filters=[("timestamp", ">=", start_ms), ("timestamp", "<=", end_ms)],
    )
    return {
        name: np.asarray(table[name].to_numpy())
        for name in ("timestamp", "open", "high", "low", "close")
    }


def baseline_corrected_pnl(
    trade: dict[str, Any],
    bars: dict[str, np.ndarray],
) -> tuple[float, float]:
    original = float(trade["pnl_pct"])
    reason = str(trade.get("exit_reason") or "")
    if reason not in {"take_profit_trailing", "stop_loss"}:
        return original, float(trade["exit_price"])
    exit_open_time = int(trade["exit_time"]) - MINUTE_MS + 1
    index = int(np.searchsorted(bars["timestamp"], exit_open_time))
    if index >= len(bars["timestamp"]) or int(bars["timestamp"][index]) != exit_open_time:
        return original, float(trade["exit_price"])
    trigger = (
        float(trade["trail_price"])
        if reason == "take_profit_trailing"
        else float(trade["stop_price"])
    )
    executable = max(trigger, float(bars["open"][index]))
    fill = executable * (1.0 + EXIT_SLIPPAGE)
    pnl = 1.0 - fill / float(trade["entry_price"]) - float(
        trade.get("fee_rate") or FEE_RATE
    )
    return pnl, fill


def simulate_guard(
    trade: dict[str, Any],
    bars: dict[str, np.ndarray],
    activation: float,
    callback: float,
    *,
    low_first: bool,
) -> tuple[int, float] | None:
    entry_time = int(trade["entry_time"])
    exit_time = int(trade["exit_time"])
    start = int(np.searchsorted(bars["timestamp"], entry_time + 1))
    baseline_exit_open = exit_time - MINUTE_MS + 1
    stop = int(np.searchsorted(bars["timestamp"], baseline_exit_open))
    if start >= stop:
        return None
    activation_price = float(trade["entry_price"]) * (1.0 - activation)
    active = False
    low_water = math.inf
    for index in range(start, min(stop, len(bars["timestamp"]))):
        open_price = float(bars["open"][index])
        high = float(bars["high"][index])
        low = float(bars["low"][index])
        timestamp = int(bars["timestamp"][index])
        if active:
            prior_trigger = low_water * (1.0 + callback)
            if open_price >= prior_trigger:
                return timestamp, open_price
            if high >= prior_trigger:
                return timestamp, prior_trigger
        if not active and low <= activation_price:
            active = True
            low_water = low
        elif active:
            low_water = min(low_water, low)
        if low_first and active:
            same_bar_trigger = low_water * (1.0 + callback)
            if high >= same_bar_trigger:
                return timestamp, max(open_price, same_bar_trigger)
    return None


def metrics(values: Iterable[float]) -> dict[str, float | int]:
    pnl = list(values)
    wins = [value for value in pnl if value > 0]
    losses = [-value for value in pnl if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    ordered = sorted(pnl)
    median = (
        ordered[len(ordered) // 2]
        if len(ordered) % 2
        else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    ) if ordered else 0.0
    return {
        "trades": len(pnl),
        "win_rate": len(wins) / len(pnl) if pnl else 0.0,
        "average_pnl_pct": sum(pnl) / len(pnl) if pnl else 0.0,
        "median_pnl_pct": median,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (99.0 if gross_profit > 0 else 0.0)
        ),
        "gross_profit_pct": gross_profit,
        "gross_loss_pct": gross_loss,
        "big_3pct_rate": sum(value >= 0.03 for value in pnl) / len(pnl) if pnl else 0.0,
        "big_5pct_rate": sum(value >= 0.05 for value in pnl) / len(pnl) if pnl else 0.0,
    }


def summarize_variant(
    rows: list[dict[str, Any]],
    key: str,
    split_ms: int,
) -> dict[str, Any]:
    all_values = [float(row[key]) for row in rows]
    train = [float(row[key]) for row in rows if int(row["entry_time"]) < split_ms]
    holdout = [float(row[key]) for row in rows if int(row["entry_time"]) >= split_ms]
    daily: dict[str, float] = defaultdict(float)
    for row in rows:
        day = datetime.fromtimestamp(
            int(row["entry_time"]) / 1000, tz=timezone.utc
        ).date().isoformat()
        daily[day] += float(row[key])
    top_days = {
        day for day, _value in sorted(daily.items(), key=lambda item: item[1], reverse=True)[:3]
    }
    ex_top = [
        float(row[key])
        for row in rows
        if datetime.fromtimestamp(
            int(row["entry_time"]) / 1000, tz=timezone.utc
        ).date().isoformat() not in top_days
    ]
    return {
        "all": metrics(all_values),
        "train": metrics(train),
        "holdout": metrics(holdout),
        "excluding_top_3_days": metrics(ex_top),
        "profitable_month_rate": profitable_month_rate(rows, key),
    }


def profitable_month_rate(rows: list[dict[str, Any]], key: str) -> float:
    monthly: dict[str, float] = defaultdict(float)
    for row in rows:
        month = datetime.fromtimestamp(
            int(row["entry_time"]) / 1000, tz=timezone.utc
        ).strftime("%Y-%m")
        monthly[month] += float(row[key])
    return (
        sum(value > 0 for value in monthly.values()) / len(monthly)
        if monthly else 0.0
    )


def utc_days(start_ms: int, end_ms: int) -> list[str]:
    start_day = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date()
    end_day = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).date()
    days = []
    current = start_day
    while current <= end_day:
        days.append(current.isoformat())
        current = current.fromordinal(current.toordinal() + 1)
    return days


def iter_agg_prices(
    agg_root: Path,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> Iterable[tuple[int, float]]:
    for day in utc_days(start_ms, end_ms):
        path = agg_root / symbol / f"{symbol}-aggTrades-{day}.zip"
        if not path.exists():
            raise FileNotFoundError(path)
        with zipfile.ZipFile(path) as archive:
            csv_name = next(name for name in archive.namelist() if name.endswith(".csv"))
            with archive.open(csv_name) as raw:
                rows = csv.DictReader(
                    (line.decode("utf-8", errors="replace") for line in raw)
                )
                for row in rows:
                    timestamp = int(row["transact_time"])
                    if timestamp < start_ms:
                        continue
                    if timestamp > end_ms:
                        break
                    yield timestamp, float(row["price"])


def simulate_exact_agg(
    trade: dict[str, Any],
    agg_root: Path,
    activation: float,
    callback: float,
) -> tuple[int, float] | None:
    active = False
    low_water = math.inf
    activation_price = float(trade["entry_price"]) * (1.0 - activation)
    for timestamp, price in iter_agg_prices(
        agg_root,
        str(trade["symbol"]),
        int(trade["entry_time"]) + 1,
        int(trade["exit_time"]),
    ):
        if not active:
            if price <= activation_price:
                active = True
                low_water = price
            continue
        low_water = min(low_water, price)
        if price >= low_water * (1.0 + callback):
            return timestamp, price
    return None


def exact_agg_audit(
    rows: list[dict[str, Any]],
    agg_root: Path,
    activation: float,
    callback: float,
    max_events: int,
) -> dict[str, Any]:
    candidates = [
        row for row in rows
        if row.get(f"upper_{activation}_{callback}_triggered")
    ]
    if len(candidates) > max_events:
        indices = np.linspace(0, len(candidates) - 1, max_events, dtype=int)
        candidates = [candidates[index] for index in indices]
    covered = 0
    exact_triggered = 0
    conservative_triggered = 0
    exact_pnl: list[float] = []
    baseline_pnl: list[float] = []
    for row in candidates:
        try:
            result = simulate_exact_agg(row, agg_root, activation, callback)
        except (FileNotFoundError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        covered += 1
        baseline = float(row["baseline_corrected_pnl"])
        baseline_pnl.append(baseline)
        if row.get(f"guard_{activation}_{callback}_triggered"):
            conservative_triggered += 1
        if result is None:
            exact_pnl.append(baseline)
            continue
        exact_triggered += 1
        _timestamp, raw_price = result
        fill = raw_price * (1.0 + EXIT_SLIPPAGE)
        exact_pnl.append(
            1.0 - fill / float(row["entry_price"]) - float(row.get("fee_rate") or FEE_RATE)
        )
    return {
        "requested_events": len(candidates),
        "covered_events": covered,
        "exact_triggered": exact_triggered,
        "conservative_triggered": conservative_triggered,
        "baseline": metrics(baseline_pnl),
        "with_exact_guard": metrics(exact_pnl),
    }


def main() -> int:
    args = parse_args()
    trades = read_trades(Path(args.trades))
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_symbol[str(trade["symbol"])].append(trade)
    activations = tuple(float(item) for item in args.activations.split(","))
    callbacks = tuple(float(item) for item in args.callbacks.split(","))

    processed: list[dict[str, Any]] = []
    for number, (symbol, symbol_trades) in enumerate(sorted(by_symbol.items()), start=1):
        path = Path(args.klines_dir) / f"{symbol}.parquet"
        if not path.exists():
            continue
        bars = load_symbol_bars(
            path,
            min(int(row["entry_time"]) for row in symbol_trades) - MINUTE_MS,
            max(int(row["exit_time"]) for row in symbol_trades),
        )
        for trade in symbol_trades:
            baseline_pnl, baseline_fill = baseline_corrected_pnl(trade, bars)
            trade["baseline_corrected_pnl"] = baseline_pnl
            trade["baseline_corrected_fill"] = baseline_fill
            for activation in activations:
                for callback in callbacks:
                    suffix = f"{activation}_{callback}"
                    conservative = simulate_guard(
                        trade, bars, activation, callback, low_first=False
                    )
                    upper = simulate_guard(
                        trade, bars, activation, callback, low_first=True
                    )
                    trade[f"guard_{suffix}_triggered"] = conservative is not None
                    trade[f"upper_{suffix}_triggered"] = upper is not None
                    if conservative is None:
                        trade[f"guard_{suffix}_pnl"] = baseline_pnl
                    else:
                        _timestamp, raw_price = conservative
                        fill = raw_price * (1.0 + EXIT_SLIPPAGE)
                        trade[f"guard_{suffix}_pnl"] = (
                            1.0
                            - fill / float(trade["entry_price"])
                            - float(trade.get("fee_rate") or FEE_RATE)
                        )
                    if upper is None:
                        trade[f"upper_{suffix}_pnl"] = baseline_pnl
                    else:
                        _timestamp, raw_price = upper
                        fill = raw_price * (1.0 + EXIT_SLIPPAGE)
                        trade[f"upper_{suffix}_pnl"] = (
                            1.0
                            - fill / float(trade["entry_price"])
                            - float(trade.get("fee_rate") or FEE_RATE)
                        )
            processed.append(trade)
        if number % 50 == 0:
            print(f"processed symbols={number}/{len(by_symbol)} trades={len(processed)}", flush=True)

    split_ms = int(
        datetime.fromisoformat(args.split_date).replace(tzinfo=timezone.utc).timestamp() * 1000
    )
    report: dict[str, Any] = {
        "method": {
            "baseline": "production trade ledger corrected when candle open already crossed a stale stop",
            "conservative": "no same-minute trigger after a new low",
            "low_first_upper": "minute low before high; optimistic upper bound only",
            "cost": {
                "round_trip_fee": FEE_RATE,
                "exit_slippage": EXIT_SLIPPAGE,
            },
        },
        "trades": len(processed),
        "baseline_original": summarize_variant(processed, "pnl_pct", split_ms),
        "baseline_corrected": summarize_variant(
            processed, "baseline_corrected_pnl", split_ms
        ),
        "variants": [],
    }
    for activation in activations:
        for callback in callbacks:
            suffix = f"{activation}_{callback}"
            conservative_key = f"guard_{suffix}_pnl"
            upper_key = f"upper_{suffix}_pnl"
            report["variants"].append({
                "activation": activation,
                "callback": callback,
                "conservative_trigger_rate": sum(
                    bool(row[f"guard_{suffix}_triggered"]) for row in processed
                ) / len(processed),
                "low_first_trigger_rate": sum(
                    bool(row[f"upper_{suffix}_triggered"]) for row in processed
                ) / len(processed),
                "conservative": summarize_variant(processed, conservative_key, split_ms),
                "low_first_upper": summarize_variant(processed, upper_key, split_ms),
            })

    baseline = report["baseline_corrected"]["holdout"]
    ranked = sorted(
        report["variants"],
        key=lambda row: (
            row["conservative"]["holdout"]["profit_factor"]
            - baseline["profit_factor"],
            row["conservative"]["holdout"]["average_pnl_pct"]
            - baseline["average_pnl_pct"],
        ),
        reverse=True,
    )
    best = ranked[0] if ranked else None
    report["best_conservative"] = best
    if best and args.agg_dir:
        report["agg_exact_audit"] = exact_agg_audit(
            processed,
            Path(args.agg_dir),
            float(best["activation"]),
            float(best["callback"]),
            int(args.agg_max_events),
        )

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "trades": report["trades"],
        "baseline_corrected": report["baseline_corrected"],
        "best_conservative": best,
        "agg_exact_audit": report.get("agg_exact_audit"),
        "output": str(output.resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
