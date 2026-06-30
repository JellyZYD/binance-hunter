from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from ..models import Candle, KlineClosed
from ..timeutils import utc_ms


def parse_combined_kline_message(raw: str | bytes) -> KlineClosed | None:
    payload = json.loads(raw)
    data = payload.get("data", payload)
    if data.get("e") != "kline":
        return None
    kline = data.get("k", {})
    if not kline.get("x"):
        return None
    candle = Candle.from_ws_kline(data)
    return KlineClosed(symbol=candle.symbol, interval=candle.interval, candle=candle, received_time=utc_ms())


class WebSocketMarketSource:
    def __init__(self, settings: dict[str, Any], symbols: list[str], intervals: list[str]):
        self.settings = settings
        self.symbols = sorted({s.upper() for s in symbols})
        self.intervals = list(intervals)
        self.base_url = settings["network"]["ws_base_url"].rstrip("/")
        self.max_streams = int(settings["websocket"]["max_streams_per_connection"])
        self.reconnect_initial = float(settings["websocket"].get("reconnect_initial_seconds", 1.0))
        self.reconnect_max = float(settings["websocket"].get("reconnect_max_seconds", 30.0))
        self._stop = asyncio.Event()

    def stream_names(self) -> list[str]:
        out = []
        for symbol in self.symbols:
            for interval in self.intervals:
                out.append(f"{symbol.lower()}@kline_{interval}")
        return out

    async def close(self) -> None:
        self._stop.set()

    async def events(self) -> AsyncIterator[KlineClosed]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets is required: pip install -r requirements.txt") from exc

        queue: asyncio.Queue[KlineClosed] = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._run_chunk(websockets, chunk, queue))
            for chunk in chunked(self.stream_names(), self.max_streams)
        ]
        try:
            while not self._stop.is_set():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_chunk(self, websockets_module: Any, streams: list[str], queue: asyncio.Queue[KlineClosed]) -> None:
        delay = self.reconnect_initial
        url = f"{self.base_url}/stream?streams={'/'.join(streams)}"
        while not self._stop.is_set():
            try:
                print(f"websocket connecting streams={len(streams)}", flush=True)
                async with websockets_module.connect(url, ping_interval=120, ping_timeout=20, close_timeout=5) as ws:
                    print(f"websocket connected streams={len(streams)}", flush=True)
                    delay = self.reconnect_initial
                    async for raw in ws:
                        event = parse_combined_kline_message(raw)
                        if event is not None:
                            await queue.put(event)
                        if self._stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"websocket reconnect after {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max, delay * 2)


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]
