"""Download Binance Vision USD-M futures bookDepth files from a job CSV."""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlretrieve


BASE_URL = "https://data.binance.vision/data/futures/um/daily/bookDepth"


@dataclass
class DownloadResult:
    symbol: str
    day: str
    status: str
    path: str
    bytes: int = 0
    error: str = ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    jobs = read_jobs(Path(args.jobs_csv))
    if args.max_jobs > 0:
        jobs = jobs[: args.max_jobs]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    removed = remove_bad_existing(jobs, out_dir) if args.validate_existing else 0
    print(json.dumps({"jobs": len(jobs), "out_dir": str(out_dir), "removed_bad_existing": removed}, ensure_ascii=False), flush=True)
    results: list[DownloadResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, job["symbol"], job["day"], out_dir, args.timeout, args.retries, args.sleep): job for job in jobs}
        done = 0
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(DownloadResult(str(job["symbol"]), str(job["day"]), "error", "", error=f"{type(exc).__name__}: {exc}"))
            done += 1
            if done % max(1, args.progress_every) == 0:
                print(progress_line(done, len(jobs), results), flush=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest = out_dir / f"bookdepth_manifest_{stamp}.csv"
    with manifest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(DownloadResult("", "", "", "")).keys()))
        writer.writeheader()
        writer.writerows(asdict(r) for r in results)
    summary = {
        "jobs": len(jobs),
        "downloaded": sum(1 for r in results if r.status == "downloaded"),
        "exists": sum(1 for r in results if r.status == "exists"),
        "missing": sum(1 for r in results if r.status == "missing"),
        "error": sum(1 for r in results if r.status == "error"),
        "bytes": sum(r.bytes for r in results),
        "removed_bad_existing": removed,
        "manifest": str(manifest),
    }
    summary_path = out_dir / f"bookdepth_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--jobs-csv", required=True)
    p.add_argument("--out-dir", default="backend/storage/bookdepth/binance_vision")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--sleep", type=float, default=0.0)
    p.add_argument("--progress-every", type=int, default=50)
    p.add_argument("--max-jobs", type=int, default=0)
    p.add_argument("--validate-existing", action="store_true")
    return p.parse_args(argv)


def read_jobs(path: Path) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            symbol = str(row.get("symbol") or "").strip().upper()
            day = str(row.get("day") or "").strip()
            if not re.fullmatch(r"[A-Z0-9]+USDT", symbol) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                continue
            key = (symbol, day)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"symbol": symbol, "day": day})
    return rows


def download_one(symbol: str, day: str, out_dir: Path, timeout: int, retries: int, sleep: float) -> DownloadResult:
    target_dir = out_dir / symbol
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{symbol}-bookDepth-{day}.zip"
    if path.exists() and path.stat().st_size > 0 and valid_zip(path):
        return DownloadResult(symbol, day, "exists", str(path), path.stat().st_size)
    path.unlink(missing_ok=True)
    url = f"{BASE_URL}/{symbol}/{symbol}-bookDepth-{day}.zip"
    last_error = ""
    for attempt in range(retries + 1):
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.unlink(missing_ok=True)
        try:
            if sleep > 0:
                time.sleep(sleep)
            # urlretrieve has no per-call timeout parameter; set global socket timeout.
            import socket

            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            try:
                urlretrieve(url, tmp)
            finally:
                socket.setdefaulttimeout(old_timeout)
            if not valid_zip(tmp):
                raise zipfile.BadZipFile("downloaded file is not a valid zip")
            tmp.replace(path)
            return DownloadResult(symbol, day, "downloaded", str(path), path.stat().st_size)
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            tmp.unlink(missing_ok=True)
            if exc.code == 404:
                return DownloadResult(symbol, day, "missing", "", error=last_error)
        except (URLError, TimeoutError, zipfile.BadZipFile, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            tmp.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(0.25 * (attempt + 1))
    return DownloadResult(symbol, day, "error", "", error=last_error)


def valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            return bool(names)
    except Exception:
        return False


def remove_bad_existing(jobs: list[dict[str, str]], out_dir: Path) -> int:
    removed = 0
    for job in jobs:
        symbol = job["symbol"]
        day = job["day"]
        path = out_dir / symbol / f"{symbol}-bookDepth-{day}.zip"
        if path.exists() and (path.stat().st_size <= 0 or not valid_zip(path)):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def progress_line(done: int, total: int, results: list[DownloadResult]) -> str:
    ok = sum(1 for r in results if r.status in {"downloaded", "exists"})
    missing = sum(1 for r in results if r.status == "missing")
    error = sum(1 for r in results if r.status == "error")
    return f"progress {done}/{total} ok={ok} missing={missing} error={error}"


if __name__ == "__main__":
    raise SystemExit(main())
