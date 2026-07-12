"""Micro-structure data collector (OI / liquidations / hot-pool depth).

Purpose: collect the derivative micro data that Binance does NOT archive
(fine-grained real-time OI, forceOrder liquidation stream, order-book top),
so future strategy research has ground truth beyond 5m Vision metrics.

Streams
-------
1. OI poll     : REST /fapi/v1/openInterest per watch symbol, every N sec
                 (default 60s; Vision metrics archives only 5m granularity).
2. Liquidation : websocket !forceOrder@arr (all market). Live-only data --
                 Binance removed historical liquidationSnapshot. Highest value.
3. Hot depth   : REST /fapi/v1/depth?limit=20 for the top-gainer hot pool
                 (default 30 symbols, every 30s): top5 levels + notional sums
                 within the book, to study bid-collapse before waterfalls.

Storage budget (defaults): OI ~450 syms x 1/min ~= 18 MB/day parquet;
depth 30 syms x 2/min ~= 6 MB/day; liquidations ~= 1-5 MB/day.
Total < 30 MB/day -> 90-day retention uses < 3 GB of the 30 GB server disk.
Hourly parquet files under storage/micro/, ring-buffer cleanup on startup
and once per day. Pull to local weekly (~200 MB) with scp/rsync.

CLI: python run.py collect-micro --broad-top 450
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ensure_dirs
from .data.rest_client import BinanceRestClient
from .discovery import build_broad_universe
from .timeutils import local_stamp, utc_ms

FAPI = "https://fapi.binance.com"


def _get_json(url: str, timeout: int = 15) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _detect_format() -> str:
    # Prefer parquet (compact, fast to analyze); fall back to gzipped CSV if no
    # parquet engine is installed, so a missing optional dep never crash-loops
    # the collector. The reader side handles both.
    try:
        import pyarrow  # noqa: F401

        return "parquet"
    except Exception:
        try:
            import fastparquet  # noqa: F401

            return "parquet"
        except Exception:
            return "csv"


class MicroWriter:
    """Buffered writer: hourly files per stream, atomic replace."""

    def __init__(self, root: Path, retention_days: int):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.buffers: dict[str, list[dict[str, Any]]] = {}
        self.last_flush = time.time()
        self.last_cleanup = 0.0
        self.fmt = _detect_format()
        self.ext = "parquet" if self.fmt == "parquet" else "csv.gz"
        if self.fmt == "csv":
            print(f"[{local_stamp()}] micro writer: no parquet engine, writing {self.ext} (pip install pyarrow for parquet)", flush=True)

    def add(self, stream: str, row: dict[str, Any]) -> None:
        self.buffers.setdefault(stream, []).append(row)

    def maybe_flush(self, force: bool = False) -> None:
        if not force and time.time() - self.last_flush < 300:
            return
        import pandas as pd

        hour = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H")
        for stream, rows in self.buffers.items():
            if not rows:
                continue
            path = self.root / f"{stream}_{hour}.{self.ext}"
            df = pd.DataFrame(rows)
            try:
                if path.exists():
                    prev = pd.read_parquet(path) if self.fmt == "parquet" else pd.read_csv(path)
                    df = pd.concat([prev, df], ignore_index=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                if self.fmt == "parquet":
                    df.to_parquet(tmp)
                else:
                    df.to_csv(tmp, index=False, compression="gzip")
                tmp.replace(path)
                rows.clear()
            except Exception as exc:
                print(f"[{local_stamp()}] micro flush {stream} failed: {exc}", flush=True)
        self.last_flush = time.time()
        if time.time() - self.last_cleanup > 86400:
            self.cleanup()

    def cleanup(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for pattern in ("*.parquet", "*.csv.gz"):
            for p in self.root.glob(pattern):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        self.last_cleanup = time.time()


async def poll_oi(writer: MicroWriter, symbols_ref: dict[str, list[str]], interval: int) -> None:
    import urllib.error

    while True:
        symbols = symbols_ref["all"]
        started = time.time()
        pace = max(0.05, interval / max(1, len(symbols)) * 0.8)
        for sym in symbols:
            try:
                data = await asyncio.to_thread(_get_json, f"{FAPI}/fapi/v1/openInterest?symbol={sym}")
                writer.add("oi", {"ts": int(data.get("time") or utc_ms()), "symbol": sym,
                                  "oi": float(data["openInterest"])})
            except urllib.error.HTTPError as exc:
                if exc.code in (418, 429):
                    # Rate-limited/banned: keep polling would extend the ban.
                    print(f"[{local_stamp()}] micro OI rate-limited ({exc.code}); backoff 200s", flush=True)
                    await asyncio.sleep(200)
                    break
            except Exception:
                pass
            await asyncio.sleep(pace)
        writer.maybe_flush()
        await asyncio.sleep(max(1.0, interval - (time.time() - started)))


async def poll_depth(writer: MicroWriter, symbols_ref: dict[str, list[str]], interval: int) -> None:
    import urllib.error

    while True:
        for sym in list(symbols_ref["hot"]):
            try:
                d = await asyncio.to_thread(_get_json, f"{FAPI}/fapi/v1/depth?symbol={sym}&limit=20")
                bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
                asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
                if not bids or not asks:
                    continue
                row: dict[str, Any] = {"ts": utc_ms(), "symbol": sym,
                                       "bid1": bids[0][0], "ask1": asks[0][0],
                                       "bid_notional20": sum(p * q for p, q in bids),
                                       "ask_notional20": sum(p * q for p, q in asks)}
                for i in range(min(5, len(bids))):
                    row[f"b{i + 1}p"], row[f"b{i + 1}q"] = bids[i]
                    row[f"a{i + 1}p"], row[f"a{i + 1}q"] = asks[i]
                writer.add("depth", row)
            except urllib.error.HTTPError as exc:
                if exc.code in (418, 429):
                    print(f"[{local_stamp()}] micro depth rate-limited ({exc.code}); backoff 200s", flush=True)
                    await asyncio.sleep(200)
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        writer.maybe_flush()
        await asyncio.sleep(interval)


async def refresh_pools(client: BinanceRestClient, settings: dict[str, Any],
                        symbols_ref: dict[str, list[str]], broad_top: int, hot_top: int) -> None:
    while True:
        try:
            rows = await asyncio.to_thread(build_broad_universe, client, settings, broad_top)
            symbols_ref["all"] = [str(r["symbol"]) for r in rows]
            tickers = await asyncio.to_thread(_get_json, f"{FAPI}/fapi/v1/ticker/24hr")
            usdt = [t for t in tickers if str(t.get("symbol", "")).endswith("USDT")]
            usdt.sort(key=lambda t: float(t.get("priceChangePercent") or 0), reverse=True)
            symbols_ref["hot"] = [str(t["symbol"]) for t in usdt[:hot_top]]
            print(f"[{local_stamp()}] micro pools all={len(symbols_ref['all'])} hot={len(symbols_ref['hot'])}", flush=True)
        except Exception as exc:
            print(f"[{local_stamp()}] micro pool refresh failed: {exc}", flush=True)
        await asyncio.sleep(900)


async def listen_liquidations(writer: MicroWriter, settings: dict[str, Any]) -> None:
    try:
        import websockets
    except ImportError:
        print("websockets not installed; liquidation stream disabled", flush=True)
        return
    base = settings.get("network", {}).get("ws_base_url", "wss://fstream.binance.com").rstrip("/")
    url = f"{base}/ws/!forceOrder@arr"
    while True:
        try:
            async with websockets.connect(url, ping_interval=120, ping_timeout=20) as ws:
                print(f"[{local_stamp()}] liquidation stream connected", flush=True)
                async for raw in ws:
                    o = json.loads(raw).get("o", {})
                    if not o:
                        continue
                    writer.add("liq", {"ts": int(o.get("T") or utc_ms()), "symbol": str(o.get("s")),
                                       "side": str(o.get("S")), "price": float(o.get("ap") or 0),
                                       "qty": float(o.get("q") or 0),
                                       "notional": float(o.get("ap") or 0) * float(o.get("q") or 0)})
        except Exception as exc:
            print(f"[{local_stamp()}] liquidation stream reconnect: {exc}", flush=True)
            await asyncio.sleep(10)


async def collect_micro(settings: dict[str, Any], broad_top: int = 450, oi_interval: int = 60,
                        depth_top: int = 30, depth_interval: int = 30, retention_days: int = 90) -> None:
    dirs = ensure_dirs(settings)
    root = Path(dirs["db"]).parent / "micro"
    writer = MicroWriter(root, retention_days)
    writer.cleanup()
    client = BinanceRestClient(settings)
    symbols_ref: dict[str, list[str]] = {"all": [], "hot": []}
    print(f"[{local_stamp()}] micro collector start -> {root} (retention {retention_days}d)", flush=True)

    async def _delayed(coro_factory, delay: float):
        # Stagger REST pollers so a co-boot with monitor prewarm can't spike
        # weight; the liquidation websocket carries no REST weight and starts now.
        await asyncio.sleep(delay)
        await coro_factory()

    tasks = [
        _delayed(lambda: refresh_pools(client, settings, symbols_ref, broad_top, depth_top), 0.0),
        _delayed(lambda: poll_oi(writer, symbols_ref, oi_interval), 90.0),
        _delayed(lambda: poll_depth(writer, symbols_ref, depth_interval), 120.0),
        listen_liquidations(writer, settings),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        writer.maybe_flush(force=True)
