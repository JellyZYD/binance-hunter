"""Search aggTrade microstructure filters for closed-1m waterfall entries.

The goal is not to replace the stable closed-1m entry.  It is to learn which
microstructure patterns inside the signal minute identify real waterfalls vs
fake breaks, using only data available at the 1m close.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pump_dump_hunter.models import KlineClosed
from pump_dump_hunter.timeutils import iso_from_ms
from pump_dump_hunter.waterfall import WaterfallEngine

from ml_experiments.compare_waterfall_replay_modes import load_klines, median
from ml_experiments.replay_aggtrade_waterfall import iter_zip_trades


MINUTE_MS = 60_000


def main() -> int:
    args = parse_args()
    settings = json.loads(Path(args.config).read_text(encoding="utf-8"))
    settings.setdefault("waterfall_quant", {})["variant"] = args.variant
    settings["waterfall_quant"]["enabled_families"] = [x.strip() for x in args.families.split(",") if x.strip()]
    symbols = discover_symbols(Path(args.agg_dir), args.symbols, args.max_symbols, args.symbol_order)
    jobs = [(s, settings, vars(args)) for s in symbols]
    rows: list[dict[str, Any]] = []
    print(json.dumps({"symbols": len(symbols), "workers": args.workers, "start": args.start, "end": args.end}, ensure_ascii=False), flush=True)
    if args.workers <= 1:
        for job in jobs:
            rows.extend(run_symbol(job))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run_symbol, job): job[0] for job in jobs}
            done = 0
            for fut in as_completed(futs):
                rows.extend(fut.result())
                done += 1
                if done % max(1, args.progress_every) == 0:
                    print(f"processed {done}/{len(jobs)} signals={len(rows)}", flush=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail_path = out_dir / f"agg_entry_signal_features_{stamp}.csv"
    write_dicts(detail_path, rows)
    summary = summarize(rows)
    search = search_filters(rows, args)
    summary_path = out_dir / f"agg_entry_signal_filter_summary_{stamp}.csv"
    write_dicts(summary_path, search)
    report = {
        "start": args.start,
        "end": args.end,
        "symbols": len(symbols),
        "signals": len(rows),
        "baseline": summary,
        "best_filters": search[:20],
        "detail_path": str(detail_path),
        "summary_path": str(summary_path),
    }
    report_path = out_dir / f"agg_entry_signal_filter_report_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="backend/config/settings.json")
    p.add_argument("--agg-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--klines-dir", default=r"E:\A\bb\data\klines")
    p.add_argument("--out-dir", default="backend/storage/ml/agg_entry_filters")
    p.add_argument("--symbols", default="")
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument("--symbol-order", choices=["name", "size_asc", "size_desc"], default="size_desc")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--variant", choices=["core", "high_pf"], default="core")
    p.add_argument("--families", default="post_pump,downtrend_continuation,momentum_dump,other")
    p.add_argument("--prewarm", type=int, default=1500)
    p.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 1)))
    p.add_argument("--progress-every", type=int, default=5)
    p.add_argument("--min-trades", type=int, default=8)
    return p.parse_args()


def run_symbol(job: tuple[str, dict[str, Any], dict[str, Any]]) -> list[dict[str, Any]]:
    symbol, settings, args = job
    start_ms = int(datetime.fromisoformat(args["start"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int((datetime.fromisoformat(args["end"]).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1
    rows = load_klines(symbol, Path(args["klines_dir"]), start_ms, end_ms, int(args["prewarm"]))
    engine = WaterfallEngine(json.loads(json.dumps(settings)))
    pre = [c for c in rows if c.close_time < start_ms][-int(args["prewarm"]):]
    engine.prime_candles(pre)
    positions_by_id: dict[str, Any] = {}
    signals: list[dict[str, Any]] = []
    for candle in rows:
        if candle.close_time < start_ms or candle.close_time > end_ms:
            continue
        _watch, changed, emitted = engine.on_kline(KlineClosed(symbol, "1m", candle))
        for pos in changed:
            if pos.status == "closed":
                positions_by_id[pos.position_id] = pos
        for sig in emitted:
            if sig.action == "open_short":
                signals.append(sig.to_dict())
    if not signals:
        return []
    wanted: dict[date, set[int]] = defaultdict(set)
    for sig in signals:
        minute_open = int(sig["decision_time"]) - MINUTE_MS + 1
        wanted[datetime.fromtimestamp(minute_open / 1000, tz=timezone.utc).date()].add(minute_open)
    micro = load_micro_features(Path(args["agg_dir"]), symbol, wanted)
    out: list[dict[str, Any]] = []
    for sig in signals:
        pos = positions_by_id.get(sig["position_id"])
        if not pos:
            continue
        minute_open = int(sig["decision_time"]) - MINUTE_MS + 1
        feat = micro.get(minute_open, {})
        out.append({
            "symbol": symbol,
            "entry_time": int(sig["decision_time"]),
            "entry_iso": iso_from_ms(int(sig["decision_time"])),
            "family": sig.get("family", ""),
            "rule": sig.get("rule", ""),
            "pnl_pct": float(pos.pnl_pct),
            "win": 1 if float(pos.pnl_pct) > 0 else 0,
            "mae_pct": float(pos.worst_price / pos.entry_price - 1.0) if pos.entry_price > 0 else 0.0,
            "mfe_pct": float(pos.entry_price / pos.best_price - 1.0) if pos.best_price > 0 else 0.0,
            **feat,
        })
    return out


def load_micro_features(agg_dir: Path, symbol: str, wanted: dict[date, set[int]]) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    for day, minutes in wanted.items():
        path = agg_dir / symbol / f"{symbol}-aggTrades-{day.isoformat()}.zip"
        if not path.exists():
            continue
        buckets = {m: new_bucket(m) for m in minutes}
        try:
            trades_iter = iter_zip_trades(path)
            for trade in trades_iter:
                ts = int(trade["time"])
                minute = ts - (ts % MINUTE_MS)
                bucket = buckets.get(minute)
                if bucket is None:
                    continue
                price = float(trade["price"])
                qty = float(trade["qty"])
                quote = price * qty
                sec = max(0, min(59, int((ts - minute) / 1000)))
                update_bucket(bucket, price, quote, bool(trade["buyer_maker"]), sec)
        except zipfile.BadZipFile:
            continue
        for minute, bucket in buckets.items():
            if bucket["trades"] > 0:
                out[minute] = finalize_bucket(bucket)
    return out


def new_bucket(minute: int) -> dict[str, Any]:
    return {
        "minute": minute,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "low_sec": 0,
        "trades": 0,
        "quote": 0.0,
        "sell_quote": 0.0,
        "last10_quote": 0.0,
        "last10_sell_quote": 0.0,
        "price_50s": 0.0,
    }


def update_bucket(b: dict[str, Any], price: float, quote: float, buyer_maker: bool, sec: int) -> None:
    if b["trades"] == 0:
        b["open"] = price
        b["high"] = price
        b["low"] = price
        b["low_sec"] = sec
    b["high"] = max(float(b["high"]), price)
    if price <= float(b["low"]):
        b["low"] = price
        b["low_sec"] = sec
    if sec >= 50 and not b["price_50s"]:
        b["price_50s"] = price
    b["close"] = price
    b["trades"] += 1
    b["quote"] += quote
    if buyer_maker:
        b["sell_quote"] += quote
    if sec >= 50:
        b["last10_quote"] += quote
        if buyer_maker:
            b["last10_sell_quote"] += quote


def finalize_bucket(b: dict[str, Any]) -> dict[str, float]:
    high = float(b["high"])
    low = float(b["low"])
    close = float(b["close"])
    open_ = float(b["open"])
    quote = float(b["quote"])
    last10_quote = float(b["last10_quote"])
    price_50s = float(b["price_50s"] or open_)
    return {
        "agg_trades": float(b["trades"]),
        "agg_quote": quote,
        "agg_sell_ratio": float(b["sell_quote"]) / quote if quote else 0.0,
        "agg_last10_sell_ratio": float(b["last10_sell_quote"]) / last10_quote if last10_quote else 0.0,
        "agg_last10_quote_share": last10_quote / quote if quote else 0.0,
        "agg_ret": close / open_ - 1.0 if open_ else 0.0,
        "agg_last10_ret": close / price_50s - 1.0 if price_50s else 0.0,
        "agg_close_pos": (close - low) / (high - low) if high > low else 0.5,
        "agg_rebound_from_low": close / low - 1.0 if low else 0.0,
        "agg_low_sec_frac": float(b["low_sec"]) / 59.0,
    }


def search_filters(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if not rows:
        return []
    specs: list[tuple[str, Any]] = []
    for v in [0.52, 0.56, 0.60, 0.64, 0.68]:
        specs.append((f"sell_ratio>={v}", lambda r, v=v: float(r.get("agg_sell_ratio", 0)) >= v))
    for v in [0.55, 0.60, 0.65, 0.70]:
        specs.append((f"last10_sell>={v}", lambda r, v=v: float(r.get("agg_last10_sell_ratio", 0)) >= v))
    for v in [0.25, 0.35, 0.45]:
        specs.append((f"close_pos<={v}", lambda r, v=v: float(r.get("agg_close_pos", 1)) <= v))
    for v in [0.003, 0.006, 0.010, 0.015]:
        specs.append((f"rebound_low<={v}", lambda r, v=v: float(r.get("agg_rebound_from_low", 9)) <= v))
    for v in [0.65, 0.75, 0.85]:
        specs.append((f"low_sec>={v}", lambda r, v=v: float(r.get("agg_low_sec_frac", 0)) >= v))
    for v in [-0.002, -0.004, -0.006, -0.010]:
        specs.append((f"last10_ret<={v}", lambda r, v=v: float(r.get("agg_last10_ret", 0)) <= v))

    candidates: list[dict[str, Any]] = []
    base = summarize(rows)
    candidates.append({"filter": "baseline", **base})
    for i, (name_a, pred_a) in enumerate(specs):
        selected = [r for r in rows if pred_a(r)]
        add_candidate(candidates, name_a, selected, int(args.min_trades))
        for name_b, pred_b in specs[i + 1:]:
            selected = [r for r in rows if pred_a(r) and pred_b(r)]
            add_candidate(candidates, f"{name_a} & {name_b}", selected, int(args.min_trades))
    candidates.sort(key=lambda r: (float(r["profit_factor"]), float(r["avg_pnl_pct"]), int(r["trades"])), reverse=True)
    return candidates


def add_candidate(out: list[dict[str, Any]], name: str, selected: list[dict[str, Any]], min_trades: int) -> None:
    if len(selected) < min_trades:
        return
    out.append({"filter": name, **summarize(selected)})


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    profit = sum(max(0.0, float(r["pnl_pct"])) for r in rows)
    loss = -sum(min(0.0, float(r["pnl_pct"])) for r in rows)
    wins = sum(1 for r in rows if float(r["pnl_pct"]) > 0)
    return {
        "trades": n,
        "win_rate": wins / n if n else 0.0,
        "avg_pnl_pct": sum(float(r["pnl_pct"]) for r in rows) / n if n else 0.0,
        "median_pnl_pct": median([float(r["pnl_pct"]) for r in rows]),
        "profit_factor": profit / loss if loss > 0 else (99.0 if profit > 0 else 0.0),
        "avg_mae_pct": sum(float(r["mae_pct"]) for r in rows) / n if n else 0.0,
        "avg_mfe_pct": sum(float(r["mfe_pct"]) for r in rows) / n if n else 0.0,
        "big_3pct": sum(1 for r in rows if float(r["pnl_pct"]) >= 0.03) / n if n else 0.0,
        "big_5pct": sum(1 for r in rows if float(r["pnl_pct"]) >= 0.05) / n if n else 0.0,
    }


def discover_symbols(root: Path, raw: str, max_symbols: int, order: str) -> list[str]:
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


def write_dicts(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
