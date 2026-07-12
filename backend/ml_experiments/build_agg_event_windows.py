"""Build targeted aggTrade download windows from local closed-1m klines.

The purpose is to avoid downloading full-market aggTrade history blindly.
We first locate waterfall-like moments from local 1m kline data, including
true waterfalls and fake breaks, then output unique symbol-day download jobs.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml_experiments.discover_waterfall_patterns import EXCLUDE, classify_family, load_symbol


MINUTE_MS = 60_000
DAY_MS = 86_400_000


@dataclass
class EventWindow:
    symbol: str
    label: str
    family: str
    event_time: int
    event_iso: str
    event_price: float
    window_start: int
    window_end: int
    agg_start_day: str
    agg_end_day: str
    future_drop_5m: float
    future_drop_15m: float
    future_drop_30m: float
    future_drop_60m: float
    adverse_5m: float
    adverse_15m: float
    t_to_low_30m: int
    ret_30m: float
    ret_2h: float
    ret_4h: float
    ret_12h: float
    ret_24h: float
    runup_24h: float
    dd_from_24h_high: float
    qv30: float
    volr20: float
    volr5_20: float
    tsell: float
    body_drop: float
    drop_2m: float
    drop_5m: float
    close_pos: float
    range_pct: float
    break_depth: float


@dataclass
class DownloadJob:
    symbol: str
    day: str
    labels: str
    event_count: int


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = parquet_files(Path(args.source), args.max_symbols)
    print(json.dumps({"files": len(paths), "source": args.source, "days": args.days}, ensure_ascii=False), flush=True)
    events: list[EventWindow] = []
    for idx, path in enumerate(paths, 1):
        try:
            df = load_symbol(path, args.days)
        except Exception as exc:
            print(f"skip {path.stem}: {type(exc).__name__}: {exc}", flush=True)
            continue
        if len(df) < 1600:
            continue
        events.extend(scan_symbol(path.stem.upper(), df, args))
        if idx % max(1, args.progress_every) == 0:
            print(f"scanned {idx}/{len(paths)} events={len(events)}", flush=True)

    events = sort_and_cap_events(events, args)
    jobs = build_download_jobs(events)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    event_path = out_dir / f"agg_event_windows_{stamp}.csv"
    job_path = out_dir / f"agg_download_jobs_{stamp}.csv"
    summary_path = out_dir / f"agg_event_windows_summary_{stamp}.json"
    write_dicts(event_path, [asdict(e) for e in events])
    write_dicts(job_path, [asdict(j) for j in jobs])
    summary = summarize(events, jobs, args, event_path, job_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=r"E:\A\bb\data\klines")
    p.add_argument("--out-dir", default="backend/storage/ml/agg_event_windows")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument("--progress-every", type=int, default=25)
    p.add_argument("--pre-min", type=int, default=30)
    p.add_argument("--post-min", type=int, default=120)
    p.add_argument("--cooldown-min", type=int, default=30)
    p.add_argument("--min-qv30", type=float, default=50_000)
    p.add_argument("--candidate-qv30", type=float, default=80_000)
    p.add_argument("--true-drop30", type=float, default=0.035)
    p.add_argument("--true-adverse5", type=float, default=0.018)
    p.add_argument("--hard-drop15", type=float, default=0.050)
    p.add_argument("--fake-max-drop30", type=float, default=0.018)
    p.add_argument("--fake-min-adverse5", type=float, default=0.025)
    p.add_argument("--max-events-per-symbol-label", type=int, default=80)
    p.add_argument("--max-total-events", type=int, default=0)
    return p.parse_args()


def parquet_files(root: Path, max_symbols: int) -> list[Path]:
    files = []
    for path in root.glob("*.parquet"):
        symbol = path.stem.upper()
        if symbol in EXCLUDE or path.name.endswith(".bak"):
            continue
        files.append(path)
    files.sort(key=lambda p: p.stem.upper())
    return files[:max_symbols] if max_symbols > 0 else files


def scan_symbol(symbol: str, df: pd.DataFrame, args: argparse.Namespace) -> list[EventWindow]:
    if len(df) < 1600:
        return []
    low = df["low"].astype(float)
    high = df["high"].astype(float)
    close = df["close"].astype(float)
    future_low_5 = low.shift(-1).rolling(5).min().shift(-4)
    future_low_15 = low.shift(-1).rolling(15).min().shift(-14)
    future_low_30 = low.shift(-1).rolling(30).min().shift(-29)
    future_low_60 = low.shift(-1).rolling(60).min().shift(-59)
    future_high_5 = high.shift(-1).rolling(5).max().shift(-4)
    future_high_15 = high.shift(-1).rolling(15).max().shift(-14)
    drop5 = close / future_low_5 - 1.0
    drop15 = close / future_low_15 - 1.0
    drop30 = close / future_low_30 - 1.0
    drop60 = close / future_low_60 - 1.0
    adverse5 = future_high_5 / close - 1.0
    adverse15 = future_high_15 / close - 1.0

    candidate = broad_candidate(df, args)
    true_mask = (
        (drop30 >= args.true_drop30)
        & (adverse5 <= args.true_adverse5)
        & (df["qv30"] >= args.min_qv30)
    ).fillna(False)
    hard_mask = (
        (drop15 >= args.hard_drop15)
        & (adverse5 <= args.fake_min_adverse5)
        & (df["qv30"] >= args.min_qv30)
    ).fillna(False)
    fake_mask = (
        candidate
        & ((drop30 <= args.fake_max_drop30) | (adverse5 >= args.fake_min_adverse5))
        & (df["qv30"] >= args.candidate_qv30)
    ).fillna(False)
    candidate_mask = (candidate & ~true_mask & ~fake_mask).fillna(False)

    out: list[EventWindow] = []
    out.extend(mask_to_events(symbol, df, true_mask | hard_mask, "true_waterfall", drop5, drop15, drop30, drop60, adverse5, adverse15, args))
    out.extend(mask_to_events(symbol, df, fake_mask, "fake_break", drop5, drop15, drop30, drop60, adverse5, adverse15, args))
    out.extend(mask_to_events(symbol, df, candidate_mask, "candidate_break", drop5, drop15, drop30, drop60, adverse5, adverse15, args))
    return out


def broad_candidate(df: pd.DataFrame, args: argparse.Namespace) -> pd.Series:
    close = df["close"].astype(float)
    prior_low = df["prior_body_low_20"].astype(float)
    body_break = close < prior_low * 0.999
    one_bar = (df["body_drop"] >= 0.006) & (df["volr20"] >= 2.0)
    two_bar = (df["drop_2m"] >= 0.010) & (df["volr20"] >= 2.0)
    five_bar = (df["drop_5m"] >= 0.018) & (df["volr5_20"] >= 1.5)
    return (
        body_break
        & (one_bar | two_bar | five_bar)
        & (df["qv30"] >= args.candidate_qv30)
        & (df["close_pos"] <= 0.45)
        & (df["tsell"] >= 0.50)
    ).fillna(False)


def mask_to_events(
    symbol: str,
    df: pd.DataFrame,
    mask: pd.Series,
    label: str,
    drop5: pd.Series,
    drop15: pd.Series,
    drop30: pd.Series,
    drop60: pd.Series,
    adverse5: pd.Series,
    adverse15: pd.Series,
    args: argparse.Namespace,
) -> list[EventWindow]:
    out: list[EventWindow] = []
    cooldown_until = -1
    idx = np.flatnonzero(mask.to_numpy())
    for i in idx:
        if i < 1500 or i >= len(df) - 65:
            continue
        if i < cooldown_until:
            continue
        row = df.iloc[i]
        future = df.iloc[i + 1: i + 31]
        t_to_low = int(np.argmin(future["low"].to_numpy())) + 1 if not future.empty else 0
        out.append(event_from_row(symbol, df, i, label, drop5, drop15, drop30, drop60, adverse5, adverse15, t_to_low, args))
        cooldown_until = int(i) + max(1, int(args.cooldown_min))
    return out


def event_from_row(
    symbol: str,
    df: pd.DataFrame,
    i: int,
    label: str,
    drop5: pd.Series,
    drop15: pd.Series,
    drop30: pd.Series,
    drop60: pd.Series,
    adverse5: pd.Series,
    adverse15: pd.Series,
    t_to_low: int,
    args: argparse.Namespace,
) -> EventWindow:
    row = df.iloc[i]
    event_time = int(row["t"])
    start = event_time - int(args.pre_min) * MINUTE_MS
    end = event_time + int(args.post_min) * MINUTE_MS
    prior_low = float(row["prior_body_low_20"]) if np.isfinite(row["prior_body_low_20"]) else float(row["close"])
    return EventWindow(
        symbol=symbol,
        label=label,
        family=classify_family(row),
        event_time=event_time,
        event_iso=iso_ms(event_time),
        event_price=float(row["close"]),
        window_start=start,
        window_end=end,
        agg_start_day=utc_day(start),
        agg_end_day=utc_day(end),
        future_drop_5m=safe_float(drop5.iloc[i]),
        future_drop_15m=safe_float(drop15.iloc[i]),
        future_drop_30m=safe_float(drop30.iloc[i]),
        future_drop_60m=safe_float(drop60.iloc[i]),
        adverse_5m=safe_float(adverse5.iloc[i]),
        adverse_15m=safe_float(adverse15.iloc[i]),
        t_to_low_30m=t_to_low,
        ret_30m=safe_float(row["ret_30m"]),
        ret_2h=safe_float(row["ret_2h"]),
        ret_4h=safe_float(row["ret_4h"]),
        ret_12h=safe_float(row["ret_12h"]),
        ret_24h=safe_float(row["ret_24h"]),
        runup_24h=safe_float(row["runup_24h"]),
        dd_from_24h_high=safe_float(row["dd_from_24h_high"]),
        qv30=safe_float(row["qv30"]),
        volr20=safe_float(row["volr20"]),
        volr5_20=safe_float(row["volr5_20"]),
        tsell=safe_float(row["tsell"]),
        body_drop=safe_float(row["body_drop"]),
        drop_2m=safe_float(row["drop_2m"]),
        drop_5m=safe_float(row["drop_5m"]),
        close_pos=safe_float(row["close_pos"]),
        range_pct=safe_float(row["range_pct"]),
        break_depth=prior_low / float(row["close"]) - 1.0 if float(row["close"]) > 0 else 0.0,
    )


def sort_and_cap_events(events: list[EventWindow], args: argparse.Namespace) -> list[EventWindow]:
    events.sort(key=lambda e: (e.symbol, e.label, e.event_time))
    capped: list[EventWindow] = []
    counts: dict[tuple[str, str], int] = {}
    for event in events:
        key = (event.symbol, event.label)
        count = counts.get(key, 0)
        if args.max_events_per_symbol_label > 0 and count >= args.max_events_per_symbol_label:
            continue
        capped.append(event)
        counts[key] = count + 1
    capped.sort(key=lambda e: (e.event_time, e.symbol, e.label))
    if args.max_total_events > 0:
        capped = capped[: args.max_total_events]
    return capped


def build_download_jobs(events: list[EventWindow]) -> list[DownloadJob]:
    raw: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        start = datetime.fromtimestamp(event.window_start / 1000, tz=timezone.utc).date()
        end = datetime.fromtimestamp(event.window_end / 1000, tz=timezone.utc).date()
        day = start
        while day <= end:
            key = (event.symbol, day.isoformat())
            item = raw.setdefault(key, {"labels": set(), "event_count": 0})
            item["labels"].add(event.label)
            item["event_count"] += 1
            day += timedelta(days=1)
    return [
        DownloadJob(symbol=symbol, day=day, labels=",".join(sorted(v["labels"])), event_count=int(v["event_count"]))
        for (symbol, day), v in sorted(raw.items())
    ]


def summarize(events: list[EventWindow], jobs: list[DownloadJob], args: argparse.Namespace, event_path: Path, job_path: Path) -> dict[str, Any]:
    df = pd.DataFrame([asdict(e) for e in events])
    by_label = {}
    by_family = {}
    if not df.empty:
        by_label = {str(k): int(len(g)) for k, g in df.groupby("label")}
        by_family = {str(k): int(len(g)) for k, g in df.groupby("family")}
    return {
        "events": len(events),
        "symbols": int(df["symbol"].nunique()) if not df.empty else 0,
        "by_label": by_label,
        "by_family": by_family,
        "download_jobs": len(jobs),
        "download_symbols": len({j.symbol for j in jobs}),
        "event_path": str(event_path),
        "job_path": str(job_path),
        "window_minutes": {"pre": args.pre_min, "post": args.post_min},
    }


def write_dicts(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def safe_float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return 0.0
    if not np.isfinite(out):
        return 0.0
    return out


if __name__ == "__main__":
    raise SystemExit(main())
