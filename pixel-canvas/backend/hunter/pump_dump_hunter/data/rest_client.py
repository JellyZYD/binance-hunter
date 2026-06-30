from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from ..models import Candle


class BinanceError(RuntimeError):
    pass


class BinanceRestClient:
    def __init__(self, settings: dict[str, Any]):
        network = settings["network"]
        self.base_urls = list(network["base_urls"])
        self.timeout = int(network["timeout_seconds"])
        self.retries = int(network["retries"])
        self.proxy = network.get("proxy") or ""
        self._selected_base: str | None = None

    def _open(self, request: Request):
        if self.proxy:
            opener = build_opener(ProxyHandler({"http": self.proxy, "https": self.proxy}))
            return opener.open(request, timeout=self.timeout)
        return urlopen(request, timeout=self.timeout)

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        query = urlencode(params, doseq=True)
        bases = [self._selected_base] if self._selected_base else []
        bases += [b for b in self.base_urls if b not in bases]
        last_error: Exception | None = None
        for base in bases:
            if not base:
                continue
            url = f"{base}{path}" + (f"?{query}" if query else "")
            for attempt in range(1, self.retries + 1):
                req = Request(url=url, method="GET")
                req.add_header("User-Agent", "binance-pump-dump-hunter/0.1")
                try:
                    with self._open(req) as resp:
                        self._selected_base = base
                        return json.loads(resp.read().decode("utf-8"))
                except HTTPError as exc:
                    payload = exc.read().decode("utf-8", errors="replace")
                    raise BinanceError(f"Binance HTTP {exc.code}: {payload}") from exc
                except (URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    if attempt < self.retries:
                        time.sleep(0.35 * attempt)
        raise BinanceError(f"Binance network error: {last_error}") from last_error

    def exchange_info(self) -> dict[str, Any]:
        return self._request("/fapi/v1/exchangeInfo")

    def ticker_24h_all(self) -> list[dict[str, Any]]:
        return self._request("/fapi/v1/ticker/24hr")

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[Candle]:
        params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        rows = self._request("/fapi/v1/klines", params)
        return [Candle.from_binance_rest(symbol, interval, row) for row in rows]
