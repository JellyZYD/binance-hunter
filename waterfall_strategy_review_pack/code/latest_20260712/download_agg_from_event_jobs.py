"""Download Binance Vision aggTrade zip files from an event job CSV."""
from __future__ import annotations

import argparse
import csv
import json
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ml_experiments.download_binance_vision_aggtrades import DownloadResult, download_one


def main() -> int:
    args = parse_args()
    jobs = read_jobs(Path(args.jobs_csv))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.max_jobs > 0:
        jobs = jobs[: args.max_jobs]
    if args.validate_existing:
        removed = remove_bad_existing(jobs, out_dir)
    else:
        removed = 0
    print(json.dumps({"jobs": len(jobs), "out_dir": str(out_dir), "removed_bad_existing": removed}, ensure_ascii=False), flush=True)
    results: list[DownloadResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(download_one, job["symbol"], date.fromisoformat(job["day"]), out_dir, args.timeout, args.retries, args.max_file_seconds): job
            for job in jobs
        }
        done = 0
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(
                    DownloadResult(
                        symbol=str(job.get("symbol") or ""),
                        day=str(job.get("day") or ""),
                        status="error",
                        path="",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            done += 1
            if done % max(1, args.progress_every) == 0:
                print(progress_line(done, len(jobs), results), flush=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest = out_dir / f"event_jobs_manifest_{stamp}.csv"
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
    summary_path = out_dir / f"event_jobs_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--jobs-csv", required=True)
    p.add_argument("--out-dir", default="backend/storage/aggtrades/binance_vision")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-file-seconds", type=int, default=900)
    p.add_argument("--progress-every", type=int, default=25)
    p.add_argument("--max-jobs", type=int, default=0)
    p.add_argument("--validate-existing", action="store_true")
    return p.parse_args()


def read_jobs(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            symbol = str(row.get("symbol") or "").strip().upper()
            day = str(row.get("day") or "").strip()
            if not symbol or not day:
                continue
            if not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
                continue
            key = (symbol, day)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"symbol": symbol, "day": day})
    return rows


def remove_bad_existing(jobs: list[dict[str, Any]], out_dir: Path) -> int:
    removed = 0
    for job in jobs:
        symbol = job["symbol"]
        day = job["day"]
        path = out_dir / symbol / f"{symbol}-aggTrades-{day}.zip"
        if not path.exists() or path.stat().st_size <= 0:
            continue
        try:
            with zipfile.ZipFile(path) as zf:
                zf.namelist()
        except zipfile.BadZipFile:
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
