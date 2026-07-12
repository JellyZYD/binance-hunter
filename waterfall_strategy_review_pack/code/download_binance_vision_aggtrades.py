"""Download Binance Vision USD-M futures aggTrade daily zip files.

The downloader is resumable: existing non-empty zip files are skipped. It is
intended for local research and replay, not for production live collection.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from http.client import IncompleteRead
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import pyarrow.parquet as pq
except Exception:  # pragma: no cover
    pq = None


BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"
CHUNK_SIZE = 2 * 1024 * 1024

DEFAULT_EXCLUDE = {
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "TRXUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
    "CLUSDT",
    "NATGASUSDT",
    "QQQUSDT",
    "SPXUSDT",
    "SPYUSDT",
    "AAPLUSDT",
    "AMZNUSDT",
    "AMDUSDT",
    "COINUSDT",
    "CRCLUSDT",
    "EWYUSDT",
    "GOOGUSDT",
    "INTCUSDT",
    "METAUSDT",
    "MSTRUSDT",
    "MSFTUSDT",
    "MUUSDT",
    "NFLXUSDT",
    "NVDAUSDT",
    "SNDKUSDT",
    "TSLAUSDT",
}


@dataclass
class DownloadResult:
    symbol: str
    day: str
    status: str
    path: str
    bytes: int = 0
    error: str = ""


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols = pick_symbols(args)
    days = list_days(args.start, args.end)
    jobs = [(symbol, day) for symbol in symbols for day in days]
    print(json.dumps({"symbols": len(symbols), "days": len(days), "jobs": len(jobs), "out_dir": str(out_dir)}, ensure_ascii=False), flush=True)

    results: list[DownloadResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(download_one, symbol, day, out_dir, args.timeout, args.retries, args.max_file_seconds): (symbol, day)
            for symbol, day in jobs
        }
        done = 0
        for fut in as_completed(futs):
            result = fut.result()
            results.append(result)
            done += 1
            if done % max(1, args.progress_every) == 0:
                ok = sum(1 for r in results if r.status in {"downloaded", "exists"})
                missing = sum(1 for r in results if r.status == "missing")
                err = sum(1 for r in results if r.status == "error")
                print(f"progress {done}/{len(jobs)} ok={ok} missing={missing} error={err}", flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest = out_dir / f"manifest_{stamp}.csv"
    with manifest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(DownloadResult("", "", "", "")).keys()))
        writer.writeheader()
        writer.writerows(asdict(r) for r in results)
    summary = {
        "symbols": len(symbols),
        "days": len(days),
        "jobs": len(jobs),
        "downloaded": sum(1 for r in results if r.status == "downloaded"),
        "exists": sum(1 for r in results if r.status == "exists"),
        "missing": sum(1 for r in results if r.status == "missing"),
        "error": sum(1 for r in results if r.status == "error"),
        "bytes": sum(r.bytes for r in results),
        "manifest": str(manifest),
    }
    (out_dir / f"summary_{stamp}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--klines-dir", default=r"E:\A\bb\data\klines")
    p.add_argument("--symbols", default="")
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-file-seconds", type=int, default=900)
    p.add_argument("--progress-every", type=int, default=20)
    return p.parse_args()


def pick_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    root = Path(args.klines_dir)
    rows: list[tuple[str, float]] = []
    if pq is None:
        raise RuntimeError("pyarrow is required to auto-pick symbols from klines")
    for path in root.glob("*.parquet"):
        symbol = path.stem.upper()
        if symbol in DEFAULT_EXCLUDE:
            continue
        try:
            table = pq.read_table(path, columns=["quote_volume"])
            qv = table.column("quote_volume")
            n = len(qv)
            tail = qv.slice(max(0, n - 1440)).to_pylist()
            rows.append((symbol, float(sum(float(x or 0.0) for x in tail))))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return [symbol for symbol, _qv in rows[: int(args.max_symbols)]]


def list_days(start: str, end: str) -> list[date]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    out = []
    d = s
    while d <= e:
        out.append(d)
        d += timedelta(days=1)
    return out


def download_one(symbol: str, day: date, out_dir: Path, timeout: int, retries: int, max_file_seconds: int) -> DownloadResult:
    day_s = day.isoformat()
    filename = f"{symbol}-aggTrades-{day_s}.zip"
    target_dir = out_dir / symbol
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists() and target.stat().st_size > 0:
        return DownloadResult(symbol, day_s, "exists", str(target), target.stat().st_size)
    url = f"{BASE_URL}/{symbol}/{filename}"
    last_error = ""
    for attempt in range(1, retries + 2):
        tmp = target.with_suffix(".zip.tmp")
        try:
            req = Request(url, headers={"User-Agent": "binance-hunter-research/1.0"})
            started = time.monotonic()
            total = 0
            with urlopen(req, timeout=timeout) as resp:
                with tmp.open("wb") as fh:
                    while True:
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        fh.write(chunk)
                        total += len(chunk)
                        if max_file_seconds > 0 and time.monotonic() - started > max_file_seconds:
                            raise TimeoutError(f"max_file_seconds={max_file_seconds} exceeded after {total} bytes")
            if total <= 0:
                return DownloadResult(symbol, day_s, "missing", str(target), 0, "empty response")
            tmp.replace(target)
            return DownloadResult(symbol, day_s, "downloaded", str(target), target.stat().st_size)
        except HTTPError as exc:
            if exc.code == 404:
                return DownloadResult(symbol, day_s, "missing", str(target), 0, "404")
            last_error = f"HTTP {exc.code}"
        except (IncompleteRead, URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        time.sleep(0.5 * attempt)
    return DownloadResult(symbol, day_s, "error", str(target), 0, last_error)


if __name__ == "__main__":
    raise SystemExit(main())
