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


def parse_combined_market_message(raw: str | bytes) -> KlineClosed | dict[str, Any] | None:
    payload = json.loads(raw)
    data = payload.get("data", payload)
    if data.get("e") == "kline":
        kline = data.get("k", {})
        if not kline.get("x"):
            return None
        candle = Candle.from_ws_kline(data)
        return KlineClosed(symbol=candle.symbol, interval=candle.interval, candle=candle, received_time=utc_ms())
    stream = str(payload.get("stream") or "")
    symbol = str(data.get("s") or "").upper()
    if not symbol:
        return None
    if "aggTrade" in stream or data.get("e") == "aggTrade":
        kind = "aggTrade"
    elif "bookTicker" in stream or data.get("e") == "bookTicker":
        kind = "bookTicker"
    else:
        return None
    return {
        "type": "micro",
        "symbol": symbol,
        "event_time": int(data.get("E") or data.get("T") or utc_ms()),
        "stream": kind,
        "payload": data,
        "created_time": utc_ms(),
    }


class WebSocketMarketSource:
    def __init__(
        self,
        settings: dict[str, Any],
        symbols: list[str],
        intervals: list[str],
        micro_streams: list[str] | None = None,
    ):
        self.settings = settings
        self.symbols = sorted({s.upper() for s in symbols})
        self.intervals = list(intervals)
        self.micro_streams = list(micro_streams or [])
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
            for stream in self.micro_streams:
                out.append(f"{symbol.lower()}@{stream}")
        return out

    async def close(self) -> None:
        self._stop.set()

    async def events(self) -> AsyncIterator[KlineClosed]:
        async for event in self._events(parse_combined_kline_message):
            yield event

    async def market_events(self) -> AsyncIterator[KlineClosed | dict[str, Any]]:
        async for event in self._events(parse_combined_market_message):
            yield event

    async def _events(self, parser: Any) -> AsyncIterator[Any]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets is required: pip install -r requirements.txt") from exc

        queue: asyncio.Queue[Any] = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._run_chunk(websockets, chunk, queue, parser))
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

    async def _run_chunk(self, websockets_module: Any, streams: list[str], queue: asyncio.Queue[Any], parser: Any) -> None:
        delay = self.reconnect_initial
        url = f"{self.base_url}/stream?streams={'/'.join(streams)}"
        while not self._stop.is_set():
            try:
                print(f"websocket connecting streams={len(streams)}", flush=True)
                async with websockets_module.connect(url, ping_interval=120, ping_timeout=20, close_timeout=5) as ws:
                    print(f"websocket connected streams={len(streams)}", flush=True)
                    delay = self.reconnect_initial
                    async for raw in ws:
                        event = parser(raw)
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
