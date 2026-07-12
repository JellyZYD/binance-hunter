"""Search aggTrade fast-entry filters for the waterfall strategy.

The closed-1m rules are stable enough to use as a baseline, but directly
running the same rules on partial aggTrade-built candles is too noisy. This
script replays the same aggTrade files once per symbol while evaluating several
fast-entry filter variants in parallel.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pump_dump_hunter.models import Candle
from pump_dump_hunter.waterfall import ExitProfile, WaterfallEngine, WaterfallPosition

from ml_experiments.replay_aggtrade_waterfall import iter_symbol_trades, partial_to_candle


MINUTE_MS = 60_000


@dataclass(frozen=True)
class AggFilterConfig:
    name: str
    exit_mode: str = "base"
    eval_ms: int = 1000
    min_age_ms: int = 0
    min_current_qv: float = 0.0
    min_current_trades: int = 0
    min_tsell: float = 0.0
    min_drop_5m: float = 0.0
    min_body_drop: float = 0.0
    max_close_pos: float = 1.0
    max_rebound_from_low: float = 9.0
    min_volr20: float = 0.0
    min_volr5_20: float = 0.0


@dataclass
class ConfigState:
    cfg: AggFilterConfig
    engine: WaterfallEngine
    last_eval: int = 0
    signals: list[dict[str, Any]] | None = None
    positions: list[WaterfallPosition] | None = None


def main() -> int:
    args = parse_args()
    settings = load_settings_json(args.config)
    settings.setdefault("waterfall_quant", {})["variant"] = args.variant
    settings.setdefault("waterfall_quant", {})["enabled_families"] = [
        x.strip() for x in args.families.split(",") if x.strip()
    ]
    configs = default_configs(args)
    symbols = discover_symbols(Path(args.agg_dir), args.symbols, args.max_symbols, args.symbol_order)
    jobs = [(symbol, settings, vars(args), [asdict(c) for c in configs]) for symbol in symbols]
    print(json.dumps({"symbols": len(symbols), "configs": len(configs), "workers": args.workers}, ensure_ascii=False), flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    partial_detail_path = out_dir / f"agg_fast_filter_partial_detail_{stamp}.csv"
    partial_summary_path = out_dir / f"agg_fast_filter_partial_summary_{stamp}.csv"
    partial_report_path = out_dir / f"agg_fast_filter_partial_report_{stamp}.md"

    rows: list[dict[str, Any]] = []
    if args.workers <= 1:
        for job in jobs:
            rows.extend(run_symbol(job))
            write_partial(rows, args, partial_detail_path, partial_summary_path, partial_report_path)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run_symbol, job): job[0] for job in jobs}
            done = 0
            for fut in as_completed(futs):
                rows.extend(fut.result())
                done += 1
                write_partial(rows, args, partial_detail_path, partial_summary_path, partial_report_path)
                if done % max(1, args.progress_every) == 0:
                    print(f"searched {done}/{len(jobs)}", flush=True)

    detail_path = out_dir / f"agg_fast_filter_detail_{stamp}.csv"
    write_dicts(detail_path, rows)
    summary = summarize(rows, args)
    summary_path = out_dir / f"agg_fast_filter_summary_{stamp}.csv"
    write_dicts(summary_path, summary)
    report_path = out_dir / f"agg_fast_filter_report_{stamp}.md"
    report_path.write_text(render_report(summary, args), encoding="utf-8")
    metrics_path = out_dir / f"agg_fast_filter_metrics_{stamp}.json"
    metrics_path.write_text(
        json.dumps(
            {
                "start": args.start,
                "end": args.end,
                "symbols": len(symbols),
                "variant": args.variant,
                "families": settings["waterfall_quant"]["enabled_families"],
                "configs": [asdict(c) for c in configs],
                "detail_path": str(detail_path),
                "summary_path": str(summary_path),
                "report_path": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"summary": str(summary_path), "report": str(report_path), "rows": len(rows)}, ensure_ascii=False), flush=True)
    if summary:
        print(json.dumps(summary[: min(8, len(summary))], ensure_ascii=False, indent=2), flush=True)
    return 0


def write_partial(rows: list[dict[str, Any]], args: argparse.Namespace, detail_path: Path, summary_path: Path, report_path: Path) -> None:
    write_dicts(detail_path, rows)
    summary = summarize(rows, args)
    write_dicts(summary_path, summary)
    report_path.write_text(render_report(summary, args), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="backend/config/settings.json")
    p.add_argument("--agg-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--klines-dir", default=r"E:\A\bb\data\klines")
    p.add_argument("--out-dir", default="backend/storage/ml/agg_fast_filter_search")
    p.add_argument("--symbols", default="")
    p.add_argument("--max-symbols", type=int, default=60)
    p.add_argument("--symbol-order", choices=["name", "size_asc", "size_desc"], default="name")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--variant", choices=["core", "high_pf"], default="core")
    p.add_argument("--families", default="post_pump,downtrend_continuation,momentum_dump")
    p.add_argument("--prewarm", type=int, default=1500)
    p.add_argument("--workers", type=int, default=max(1, min(2, (os.cpu_count() or 4) - 1)))
    p.add_argument("--config-names", default="", help="Comma-separated subset of generated config names")
    p.add_argument("--progress-every", type=int, default=5)
    return p.parse_args()


def default_configs(args: argparse.Namespace) -> list[AggFilterConfig]:
    configs = [
        AggFilterConfig("direct_1s", eval_ms=1000),
        AggFilterConfig("age20_base", min_age_ms=20_000),
        AggFilterConfig("age30_base", min_age_ms=30_000),
        AggFilterConfig("age20_sell65_base", min_age_ms=20_000, min_tsell=0.65),
        AggFilterConfig("age20_sell70_base", min_age_ms=20_000, min_tsell=0.70),
        AggFilterConfig("age20_no_rebound08_base", min_age_ms=20_000, max_rebound_from_low=0.008),
        AggFilterConfig("age20_sell65_no_rebound08_base", min_age_ms=20_000, min_tsell=0.65, max_rebound_from_low=0.008),
        AggFilterConfig("age20_sell65_drop3_base", min_age_ms=20_000, min_tsell=0.65, min_drop_5m=0.030),
        AggFilterConfig(
            "age20_strict_base",
            min_age_ms=20_000,
            min_current_qv=100_000,
            min_tsell=0.65,
            min_drop_5m=0.030,
            max_close_pos=0.30,
            max_rebound_from_low=0.008,
        ),
        AggFilterConfig("age30_sell65_no_rebound08_base", min_age_ms=30_000, min_tsell=0.65, max_rebound_from_low=0.008),
        AggFilterConfig("age30_strict_base", min_age_ms=30_000, min_current_qv=100_000, min_tsell=0.65, min_drop_5m=0.030, max_rebound_from_low=0.008),
        AggFilterConfig("age20_sell65_no_rebound08_tight", exit_mode="tight", min_age_ms=20_000, min_tsell=0.65, max_rebound_from_low=0.008),
        AggFilterConfig("age20_sell65_no_rebound08_fastlock", exit_mode="fast_lock", min_age_ms=20_000, min_tsell=0.65, max_rebound_from_low=0.008),
        AggFilterConfig("age20_sell65_no_rebound08_let", exit_mode="let_run", min_age_ms=20_000, min_tsell=0.65, max_rebound_from_low=0.008),
        AggFilterConfig("age20_strict_tight", exit_mode="tight", min_age_ms=20_000, min_current_qv=100_000, min_tsell=0.65, min_drop_5m=0.030, max_close_pos=0.30, max_rebound_from_low=0.008),
        AggFilterConfig("age20_strict_fastlock", exit_mode="fast_lock", min_age_ms=20_000, min_current_qv=100_000, min_tsell=0.65, min_drop_5m=0.030, max_close_pos=0.30, max_rebound_from_low=0.008),
        AggFilterConfig("age30_strict_tight", exit_mode="tight", min_age_ms=30_000, min_current_qv=100_000, min_tsell=0.65, min_drop_5m=0.030, max_rebound_from_low=0.008),
        AggFilterConfig("age30_strict_fastlock", exit_mode="fast_lock", min_age_ms=30_000, min_current_qv=100_000, min_tsell=0.65, min_drop_5m=0.030, max_rebound_from_low=0.008),
    ]
    wanted = {x.strip() for x in str(args.config_names or "").split(",") if x.strip()}
    if wanted:
        configs = [cfg for cfg in configs if cfg.name in wanted]
        missing = sorted(wanted - {cfg.name for cfg in configs})
        if missing:
            raise ValueError(f"unknown config names: {', '.join(missing)}")
    return configs


def run_symbol(job: tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    symbol, settings, args, raw_configs = job
    configs = [AggFilterConfig(**c) for c in raw_configs]
    start_ms = int(datetime.fromisoformat(args["start"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int((datetime.fromisoformat(args["end"]).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1
    context = load_context_klines(symbol, Path(args["klines_dir"]), start_ms, int(args["prewarm"]))
    states: list[ConfigState] = []
    for cfg in configs:
        engine = WaterfallEngine(json.loads(json.dumps(settings)))
        apply_exit_mode(engine, cfg.exit_mode)
        engine.prime_candles(context)
        states.append(ConfigState(cfg=cfg, engine=engine, signals=[], positions=[]))

    processed = 0
    partial: dict[str, Any] | None = None
    start_day = date.fromisoformat(args["start"])
    end_day = date.fromisoformat(args["end"])
    for trade in iter_symbol_trades(Path(args["agg_dir"]), symbol, start_day, end_day):
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
                final_candle = partial_to_candle(symbol, partial, int(partial["open_time"]) + MINUTE_MS - 1)
                for state in states:
                    state.engine._append(final_candle)
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
        for state in states:
            pos = state.engine.positions.get(symbol)
            if pos:
                exit_signal = state.engine.update_position(pos, tick)
                if exit_signal:
                    assert state.signals is not None and state.positions is not None
                    state.signals.append(exit_signal.to_dict())
                    state.positions.append(pos)
                    state.engine.positions.pop(symbol, None)

            cfg = state.cfg
            if ts - state.last_eval < cfg.eval_ms:
                continue
            state.last_eval = ts
            age = ts - int(partial["open_time"])
            if age < cfg.min_age_ms:
                continue
            current = partial_to_candle(symbol, partial, ts)
            state.engine._append(current)
            if symbol in state.engine.positions:
                continue
            feat = state.engine.features(symbol)
            if not feat or not extra_filter_ok(feat, current, cfg):
                continue
            entry = state.engine.entry_signal(symbol, feat, current)
            if entry:
                pos, signal = entry
                assert state.signals is not None
                state.engine.positions[symbol] = pos
                state.signals.append(signal.to_dict())

    rows: list[dict[str, Any]] = []
    days = (date.fromisoformat(args["end"]) - date.fromisoformat(args["start"])).days + 1
    for state in states:
        assert state.signals is not None and state.positions is not None
        rows.append({
            "symbol": symbol,
            "config": state.cfg.name,
            "_days": days,
            "agg_trades": processed,
            **summarize_positions(state.positions, state.signals),
        })
    return rows


def extra_filter_ok(feat: dict[str, float], candle: Candle, cfg: AggFilterConfig) -> bool:
    rebound_from_low = candle.close / candle.low - 1.0 if candle.low > 0 else 0.0
    if candle.quote_volume < cfg.min_current_qv:
        return False
    if candle.trades < cfg.min_current_trades:
        return False
    if feat["tsell"] < cfg.min_tsell:
        return False
    if feat["drop_5m"] < cfg.min_drop_5m:
        return False
    if feat["body_drop"] < cfg.min_body_drop:
        return False
    if feat["close_pos"] > cfg.max_close_pos:
        return False
    if rebound_from_low > cfg.max_rebound_from_low:
        return False
    if feat["volr20"] < cfg.min_volr20:
        return False
    if feat["volr5_20"] < cfg.min_volr5_20:
        return False
    return True


def apply_exit_mode(engine: WaterfallEngine, mode: str) -> None:
    if mode == "base":
        return

    def set_profile(key: str, name: str, stop: float, trail_on: float, trail_back: float, quick: float, rebound_on: float, rebound_back: float, hold: int) -> None:
        item = ExitProfile(name, stop, 0.0030, trail_on, trail_back, quick, rebound_on, rebound_back, hold)
        engine.profiles[key] = item
        engine.profiles[name] = item

    if mode == "tight":
        set_profile("medium_30_lock", "agg_tight_22", 0.022, 0.024, 0.007, 0.0020, 0.020, 0.012, 120)
        set_profile("medium_28_lock", "agg_tight_20", 0.020, 0.022, 0.0065, 0.0018, 0.018, 0.011, 120)
        set_profile("dynamic_step_like", "agg_tight_dyn", 0.022, 0.026, 0.007, 0.0020, 0.020, 0.011, 150)
        set_profile("let_big_run", "agg_tight_run", 0.026, 0.034, 0.010, 0.0025, 0.028, 0.018, 180)
    elif mode == "fast_lock":
        set_profile("medium_30_lock", "agg_fastlock_28", 0.028, 0.020, 0.006, 0.0025, 0.018, 0.010, 150)
        set_profile("medium_28_lock", "agg_fastlock_26", 0.026, 0.019, 0.006, 0.0022, 0.017, 0.010, 150)
        set_profile("dynamic_step_like", "agg_fastlock_dyn", 0.028, 0.022, 0.006, 0.0022, 0.018, 0.010, 180)
        set_profile("let_big_run", "agg_fastlock_run", 0.032, 0.030, 0.009, 0.0030, 0.026, 0.016, 240)
    elif mode == "let_run":
        set_profile("medium_30_lock", "agg_let_32", 0.032, 0.042, 0.012, 0.0035, 0.035, 0.022, 240)
        set_profile("medium_28_lock", "agg_let_30", 0.030, 0.040, 0.011, 0.0030, 0.032, 0.020, 240)
        set_profile("dynamic_step_like", "agg_let_dyn", 0.032, 0.045, 0.012, 0.0030, 0.034, 0.020, 300)
        set_profile("let_big_run", "agg_let_run", 0.038, 0.060, 0.018, 0.0040, 0.050, 0.030, 420)
    else:
        raise ValueError(f"unknown exit_mode={mode!r}")


def load_settings_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def discover_symbols(root: Path, raw: str, max_symbols: int, order: str = "name") -> list[str]:
    if raw:
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        dirs = [p for p in root.iterdir() if p.is_dir() and any(p.glob("*.zip"))]
        if order in {"size_asc", "size_desc"}:
            dirs.sort(key=lambda p: symbol_zip_bytes(p), reverse=(order == "size_desc"))
        else:
            dirs.sort(key=lambda p: p.name.upper())
        symbols = [p.name.upper() for p in dirs]
    return symbols[:max_symbols] if max_symbols > 0 else symbols


def symbol_zip_bytes(symbol_dir: Path) -> int:
    return sum(p.stat().st_size for p in symbol_dir.glob("*.zip"))


def load_context_klines(symbol: str, klines_dir: Path, start_ms: int, prewarm: int) -> list[Candle]:
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
    keep: deque[Candle] = deque(maxlen=max(1, prewarm))
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=65_536, columns=columns):
        data = batch.to_pydict()
        timestamps = data["timestamp"]
        for i, raw_ts in enumerate(timestamps):
            ts = int(raw_ts)
            if ts >= start_ms:
                return list(keep)
            keep.append(
                Candle(
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
            )
    return list(keep)


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


def summarize(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_config = sorted({str(r["config"]) for r in rows})
    for config in by_config:
        group = [r for r in rows if r["config"] == config]
        trades = int(sum(int(r["trades"]) for r in group))
        signals = int(sum(int(r["signals"]) for r in group))
        days = int(group[0].get("_days", 1)) if group else 1
        profit = sum(float(r["gross_profit"]) for r in group)
        loss = sum(float(r["gross_loss"]) for r in group)
        wins = sum(float(r["wins"]) for r in group)
        weighted = lambda key: sum(float(r[key]) * int(r["trades"]) for r in group) / trades if trades else 0.0
        out.append({
            "config": config,
            "symbols": len(group),
            "signals": signals,
            "trades": trades,
            "trades_per_day": trades / max(1, days),
            "win_rate": wins / trades if trades else 0.0,
            "avg_pnl_pct": weighted("avg_pnl_pct"),
            "median_symbol_pnl_pct": median([float(r["median_pnl_pct"]) for r in group if int(r["trades"]) > 0]),
            "profit_factor": profit / loss if loss > 0 else (99.0 if profit > 0 else 0.0),
            "avg_mae_pct": weighted("avg_mae_pct"),
            "avg_mfe_pct": weighted("avg_mfe_pct"),
            "big_3pct": weighted("big_3pct"),
            "big_5pct": weighted("big_5pct"),
            "gross_profit": profit,
            "gross_loss": loss,
        })
    out.sort(key=lambda r: (float(r["profit_factor"]), float(r["avg_pnl_pct"]), int(r["trades"])), reverse=True)
    return out


def render_report(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    lines = ["# aggTrade Fast Filter Search", ""]
    lines.append(f"Window: {args.start} to {args.end}")
    lines.append(f"Variant: {args.variant}")
    lines.append(f"Families: {args.families}")
    lines.append("")
    if not rows:
        lines.append("No results.")
        return "\n".join(lines)
    lines.append("| config | trades/day | trades | win | PF | avg | median | MAE | MFE | 3%+ | 5%+ |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        lines.append(
            f"| {row['config']} | {float(row['trades_per_day']):.2f} | {int(row['trades'])} | "
            f"{float(row['win_rate']) * 100:.1f}% | {float(row['profit_factor']):.3f} | "
            f"{float(row['avg_pnl_pct']) * 100:.2f}% | {float(row['median_symbol_pnl_pct']) * 100:.2f}% | "
            f"{float(row['avg_mae_pct']) * 100:.2f}% | {float(row['avg_mfe_pct']) * 100:.2f}% | "
            f"{float(row['big_3pct']) * 100:.1f}% | {float(row['big_5pct']) * 100:.1f}% |"
        )
    return "\n".join(lines)


def median(values: list[float]) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


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
