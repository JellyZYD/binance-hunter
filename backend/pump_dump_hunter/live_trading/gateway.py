from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import requests

from .config import LiveTradingConfig
from .credentials import BinanceCredentials, redact_secret


class GatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: int | str | None = None,
        status: int | None = None,
        endpoint: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.endpoint = endpoint


class UnknownExecutionStatus(GatewayError):
    pass


class RateLimitError(GatewayError):
    pass


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def clean_params(params: dict[str, Any] | None) -> dict[str, str]:
    return {key: _value(value) for key, value in (params or {}).items() if value is not None}


def hmac_signature(secret: str, params: dict[str, Any], sort_keys: bool = False) -> str:
    items = sorted(params.items()) if sort_keys else list(params.items())
    payload = urlencode([(key, _value(value)) for key, value in items])
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class GatewayResponse:
    data: Any
    status: int
    headers: dict[str, str]


class BinanceSignedRest:
    """USD-M/Portfolio Margin REST client with pooled read connections."""

    def __init__(self, config: LiveTradingConfig, credentials: BinanceCredentials):
        self.config = config
        self.credentials = credentials
        self.base_url = config.rest_base_url
        self.market_base_url = config.market_base_url
        self.time_offset_ms = 0
        self.last_headers: dict[str, str] = {}
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=4,
                pool_maxsize=4,
                max_retries=0,
                pool_block=True,
            )
            session.mount("https://", adapter)
            session.headers.update({"User-Agent": "binance-hunter-live/0.1"})
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _discard_thread_session(self) -> None:
        session = getattr(self._local, "session", None)
        if session is None:
            return
        try:
            session.close()
        finally:
            self._local.session = None
            with self._sessions_lock:
                if session in self._sessions:
                    self._sessions.remove(session)

    def close(self) -> None:
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        api_key_only: bool = False,
        execution: bool = False,
        timeout: float = 8.0,
        base_url: str | None = None,
    ) -> GatewayResponse:
        method_upper = method.upper()
        max_attempts = 4 if method_upper == "GET" and not execution else 1
        for attempt in range(max_attempts):
            values = clean_params(params)
            if signed:
                values["recvWindow"] = str(self.config.recv_window_ms)
                values["timestamp"] = str(self._timestamp())
                values["signature"] = hmac_signature(self.credentials.api_secret, values)
            query = urlencode(list(values.items()))
            url = f"{base_url or self.base_url}{path}" + (f"?{query}" if query else "")
            headers: dict[str, str] = {}
            if signed or api_key_only:
                headers["X-MBX-APIKEY"] = self.credentials.api_key
            try:
                response = self._session().request(
                    method_upper,
                    url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                )
                payload = response.text
                self.last_headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status_code < 400:
                    return GatewayResponse(
                        json.loads(payload) if payload else {},
                        int(response.status_code),
                        self.last_headers,
                    )
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    data = {"msg": payload[:500]}
                code = data.get("code")
                message = redact_secret(str(data.get("msg") or payload[:500]), self.credentials)
                if response.status_code in (418, 429):
                    raise RateLimitError(
                        f"{method_upper} {path}: {message}",
                        code=code,
                        status=response.status_code,
                        endpoint=path,
                    )
                if code == -1021 and signed and not execution and attempt + 1 < max_attempts:
                    # Read-only reconciliation is safe to retry after correcting
                    # clock skew. Execution requests remain single-attempt.
                    self.sync_time()
                    continue
                if response.status_code >= 500 and attempt + 1 < max_attempts:
                    time.sleep(0.25 * (2**attempt) + random.uniform(0.0, 0.10))
                    continue
                if execution and response.status_code >= 500:
                    raise UnknownExecutionStatus(
                        f"{method_upper} {path}: {message}",
                        code=code,
                        status=response.status_code,
                        endpoint=path,
                    )
                raise GatewayError(
                    f"{method_upper} {path}: {message}",
                    code=code,
                    status=response.status_code,
                    endpoint=path,
                )
            except requests.RequestException as exc:
                self._discard_thread_session()
                if attempt + 1 < max_attempts:
                    time.sleep(0.25 * (2**attempt) + random.uniform(0.0, 0.10))
                    continue
                message = redact_secret(
                    f"{method_upper} {path}: {type(exc).__name__}: {exc}",
                    self.credentials,
                )
                if execution:
                    raise UnknownExecutionStatus(message, endpoint=path) from exc
                raise GatewayError(message, endpoint=path) from exc
        raise GatewayError(
            f"request attempts exhausted: {method_upper} {path}", endpoint=path,
        )

    def sync_time(self) -> int:
        before = int(time.time() * 1000)
        result = self._request(
            "GET", "/fapi/v1/time", base_url=self.market_base_url,
        ).data
        after = int(time.time() * 1000)
        server_time = int(result["serverTime"])
        # Use the response-arrival bound instead of the RTT midpoint. With an
        # asymmetric proxy the midpoint can place our timestamp >1s in Binance's
        # future, which recvWindow cannot forgive. This bound is deliberately
        # behind server time; recvWindow absorbs the response-path latency.
        self.time_offset_ms = server_time - after
        return self.time_offset_ms

    def exchange_info(self) -> dict[str, Any]:
        return self._request(
            "GET", "/fapi/v1/exchangeInfo", base_url=self.market_base_url,
        ).data

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        return self._request(
            "GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol.upper()},
            base_url=self.market_base_url,
        ).data

    def depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        return self._request(
            "GET", "/fapi/v1/depth", {"symbol": symbol.upper(), "limit": limit},
            base_url=self.market_base_url,
        ).data

    def account_info(self) -> dict[str, Any]:
        if self.config.account_api != "portfolio_margin":
            return self._request("GET", "/fapi/v3/account", signed=True).data
        account = dict(self._request("GET", "/papi/v1/account", signed=True).data)
        balances = list(self._request("GET", "/papi/v1/balance", signed=True).data)
        usdt = next((row for row in balances if str(row.get("asset")) == "USDT"), {})
        account["totalWalletBalance"] = str(
            account.get("actualEquity") or usdt.get("totalWalletBalance") or "0"
        )
        account["availableBalance"] = str(account.get("totalAvailableBalance") or "0")
        account["totalMarginBalance"] = str(account.get("accountEquity") or "0")
        account["totalUnrealizedProfit"] = str(
            Decimal(str(usdt.get("umUnrealizedPNL") or "0"))
            + Decimal(str(usdt.get("cmUnrealizedPNL") or "0"))
        )
        account["totalMaintMargin"] = str(account.get("accountMaintMargin") or "0")
        account["portfolioMarginBalances"] = balances
        return account

    def position_mode(self) -> dict[str, Any]:
        path = (
            "/papi/v1/um/positionSide/dual"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v1/positionSide/dual"
        )
        return self._request("GET", path, signed=True).data

    def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        path = (
            "/papi/v1/um/positionRisk"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v3/positionRisk"
        )
        return self._request("GET", path, {"symbol": symbol}, signed=True).data

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        path = (
            "/papi/v1/um/openOrders"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v1/openOrders"
        )
        return self._request("GET", path, {"symbol": symbol}, signed=True).data

    def open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.config.account_api == "portfolio_margin":
            return self._request(
                "GET", "/papi/v1/um/algo/openAlgoOrders",
                {"algoType": "CONDITIONAL", "symbol": symbol}, signed=True,
            ).data
        return self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True).data

    def reconcile_snapshot(self) -> dict[str, Any]:
        """Read the four authoritative trading states on one pooled session."""
        return {
            "account": self.account_info(),
            "positions": self.position_risk(),
            "open_orders": self.open_orders(),
            "open_algo_orders": self.open_algo_orders(),
        }

    def preflight_snapshot(self) -> dict[str, Any]:
        """Build a startup snapshot sequentially on one persistent session."""
        offset = self.sync_time()
        snapshot = self.reconcile_snapshot()
        snapshot["time_offset_ms"] = offset
        snapshot["position_mode"] = self.position_mode()
        return snapshot

    def query_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        path = "/papi/v1/um/order" if self.config.account_api == "portfolio_margin" else "/fapi/v1/order"
        return self._request(
            "GET", path,
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
        ).data

    def query_algo_order(
        self, *, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> dict[str, Any]:
        path = (
            "/papi/v1/um/algo/algoOrder"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v1/algoOrder"
        )
        return self._request(
            "GET", path,
            {"algoId": algo_id, "clientAlgoId": client_algo_id},
            signed=True,
        ).data

    def user_trades(self, symbol: str, start_time: int | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        path = "/papi/v1/um/userTrades" if self.config.account_api == "portfolio_margin" else "/fapi/v1/userTrades"
        return self._request(
            "GET", path,
            {"symbol": symbol, "startTime": start_time, "limit": limit}, signed=True,
        ).data

    def income(self, start_time: int | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        path = "/papi/v1/um/income" if self.config.account_api == "portfolio_margin" else "/fapi/v1/income"
        return self._request(
            "GET", path, {"startTime": start_time, "limit": limit}, signed=True,
        ).data

    def commission_rate(self, symbol: str) -> dict[str, Any]:
        path = (
            "/papi/v1/um/commissionRate"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v1/commissionRate"
        )
        return self._request("GET", path, {"symbol": symbol}, signed=True).data

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        path = "/papi/v1/um/leverage" if self.config.account_api == "portfolio_margin" else "/fapi/v1/leverage"
        return self._request(
            "POST", path, {"symbol": symbol, "leverage": leverage},
            signed=True, execution=True,
        ).data

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict[str, Any]:
        if self.config.account_api == "portfolio_margin":
            raise GatewayError("Portfolio Margin does not support per-symbol margin type")
        return self._request(
            "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type},
            signed=True, execution=True,
        ).data

    def start_listen_key(self) -> str:
        path = "/papi/v1/listenKey" if self.config.account_api == "portfolio_margin" else "/fapi/v1/listenKey"
        data = self._request("POST", path, api_key_only=True).data
        return str(data["listenKey"])

    def keepalive_listen_key(self, listen_key: str) -> None:
        path = "/papi/v1/listenKey" if self.config.account_api == "portfolio_margin" else "/fapi/v1/listenKey"
        params = None if self.config.account_api == "portfolio_margin" else {"listenKey": listen_key}
        self._request("PUT", path, params, api_key_only=True)

    def close_listen_key(self, listen_key: str) -> None:
        path = "/papi/v1/listenKey" if self.config.account_api == "portfolio_margin" else "/fapi/v1/listenKey"
        params = None if self.config.account_api == "portfolio_margin" else {"listenKey": listen_key}
        self._request("DELETE", path, params, api_key_only=True)

    def place_order_rest(self, params: dict[str, Any]) -> dict[str, Any]:
        path = "/papi/v1/um/order" if self.config.account_api == "portfolio_margin" else "/fapi/v1/order"
        return self._request("POST", path, params, signed=True, execution=True).data

    def place_algo_rest(self, params: dict[str, Any]) -> dict[str, Any]:
        path = (
            "/papi/v1/um/algo/order"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v1/algoOrder"
        )
        return self._request("POST", path, params, signed=True, execution=True).data

    def cancel_order_rest(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        path = "/papi/v1/um/order" if self.config.account_api == "portfolio_margin" else "/fapi/v1/order"
        return self._request(
            "DELETE", path,
            {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True, execution=True,
        ).data

    def cancel_algo_rest(self, client_algo_id: str) -> dict[str, Any]:
        path = (
            "/papi/v1/um/algo/order"
            if self.config.account_api == "portfolio_margin"
            else "/fapi/v1/algoOrder"
        )
        return self._request(
            "DELETE", path, {"clientAlgoId": client_algo_id},
            signed=True, execution=True,
        ).data


class BinanceTradeWebSocket:
    def __init__(self, config: LiveTradingConfig, credentials: BinanceCredentials):
        self.config = config
        self.credentials = credentials
        self.ws: Any = None
        self.receiver_task: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.time_offset_ms = 0
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.ws is not None:
            return
        import websockets

        self.ws = await websockets.connect(
            self.config.ws_trade_url,
            ping_interval=120,
            ping_timeout=20,
            close_timeout=5,
            max_queue=1024,
        )
        self.receiver_task = asyncio.create_task(self._receive_loop(), name="binance-trade-ws-receiver")

    async def close(self) -> None:
        if self.receiver_task:
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except (asyncio.CancelledError, Exception):
                pass
            self.receiver_task = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        for future in self.pending.values():
            if not future.done():
                future.set_exception(UnknownExecutionStatus("trade websocket closed with request pending"))
        self.pending.clear()

    async def _receive_loop(self) -> None:
        try:
            async for raw in self.ws:
                payload = json.loads(raw)
                request_id = str(payload.get("id") or "")
                future = self.pending.pop(request_id, None)
                if future and not future.done():
                    future.set_result(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = UnknownExecutionStatus(f"trade websocket receive failed: {type(exc).__name__}: {exc}")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            self.ws = None

    async def request(self, method: str, params: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        await self.connect()
        values = clean_params(params)
        values["apiKey"] = self.credentials.api_key
        values["recvWindow"] = str(self.config.recv_window_ms)
        values["timestamp"] = str(int(time.time() * 1000) + self.time_offset_ms)
        values["signature"] = hmac_signature(self.credentials.api_secret, values, sort_keys=True)
        request_id = uuid.uuid4().hex
        payload = {"id": request_id, "method": method, "params": values}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[request_id] = future
        try:
            async with self._send_lock:
                await self.ws.send(json.dumps(payload, separators=(",", ":")))
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.CancelledError:
            self.pending.pop(request_id, None)
            raise
        except asyncio.TimeoutError as exc:
            self.pending.pop(request_id, None)
            raise UnknownExecutionStatus(f"{method} timed out; execution status unknown") from exc
        except Exception:
            self.pending.pop(request_id, None)
            raise
        status = int(response.get("status") or 0)
        if status >= 400 or response.get("error"):
            error = response.get("error") or {}
            code = error.get("code")
            message = redact_secret(str(error.get("msg") or error), self.credentials)
            if status in (418, 429):
                raise RateLimitError(message, code=code, status=status)
            if status >= 500:
                raise UnknownExecutionStatus(message, code=code, status=status)
            raise GatewayError(message, code=code, status=status)
        return dict(response.get("result") or {})

    async def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.request("order.place", params)

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await self.request("order.cancel", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def place_algo(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.request("algoOrder.place", params)

    async def cancel_algo(self, client_algo_id: str) -> dict[str, Any]:
        return await self.request("algoOrder.cancel", {"clientAlgoId": client_algo_id})


class BinanceRestTradeTransport:
    """Portfolio Margin execution transport using the supported PAPI REST routes."""

    def __init__(self, rest: BinanceSignedRest):
        self.rest = rest
        self.time_offset_ms = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.rest.place_order_rest, params)

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.rest.cancel_order_rest, symbol, client_order_id)

    async def place_algo(self, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.rest.place_algo_rest, params)

    async def cancel_algo(self, client_algo_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.rest.cancel_algo_rest, client_algo_id)


class BinanceUserDataStream:
    """Private user stream for USD-M or Portfolio Margin accounts."""

    def __init__(self, config: LiveTradingConfig, rest: BinanceSignedRest):
        self.config = config
        self.rest = rest
        self.listen_key = ""
        self.ws: Any = None
        self.keepalive_task: asyncio.Task | None = None
        self.last_event_time_ms = 0

    async def connect(self) -> None:
        import websockets

        self.listen_key = await asyncio.to_thread(self.rest.start_listen_key)
        if self.config.account_api == "portfolio_margin":
            stream_url = f"{self.config.ws_stream_url}/ws/{self.listen_key}"
        else:
            stream_url = f"{self.config.ws_stream_url}/private/stream"
        stale_seconds = max(5, int(self.config.private_stream_stale_seconds))
        self.ws = await websockets.connect(
            stream_url,
            ping_interval=max(5, stale_seconds / 2),
            ping_timeout=stale_seconds,
            close_timeout=5,
            max_queue=4096,
        )
        if self.config.account_api != "portfolio_margin":
            await self.ws.send(json.dumps({
                "method": "SUBSCRIBE", "params": [self.listen_key], "id": uuid.uuid4().hex,
            }))
        self.last_event_time_ms = int(time.time() * 1000)
        self.keepalive_task = asyncio.create_task(self._keepalive(), name="binance-user-stream-keepalive")

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(45 * 60)
            await asyncio.to_thread(self.rest.keepalive_listen_key, self.listen_key)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if self.ws is None:
            await self.connect()
        async for raw in self.ws:
            payload = json.loads(raw)
            if "result" in payload and "id" in payload:
                continue
            self.last_event_time_ms = int(time.time() * 1000)
            data = payload.get("data", payload)
            if isinstance(data, dict):
                yield data

    async def close(self) -> None:
        if self.keepalive_task:
            self.keepalive_task.cancel()
            try:
                await self.keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self.keepalive_task = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        if self.listen_key:
            try:
                await asyncio.to_thread(self.rest.close_listen_key, self.listen_key)
            except Exception:
                pass
            self.listen_key = ""


class BinanceGateway:
    def __init__(self, config: LiveTradingConfig, credentials: BinanceCredentials):
        self.config = config
        self.credentials = credentials
        self.rest = BinanceSignedRest(config, credentials)
        self.trade_ws = (
            BinanceRestTradeTransport(self.rest)
            if config.account_api == "portfolio_margin"
            else BinanceTradeWebSocket(config, credentials)
        )
        self.user_stream = BinanceUserDataStream(config, self.rest)

    async def preflight_connect(self, connect_trade_ws: bool = False) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(self.rest.preflight_snapshot)
        offset = int(snapshot["time_offset_ms"])
        self.trade_ws.time_offset_ms = offset
        if connect_trade_ws:
            await self.trade_ws.connect()
        return {
            "time_offset_ms": offset,
            "account": snapshot["account"],
            "position_mode": snapshot["position_mode"],
            "positions": snapshot["positions"],
            "open_orders": snapshot["open_orders"],
            "open_algo_orders": snapshot["open_algo_orders"],
        }

    async def close(self) -> None:
        await self.user_stream.close()
        await self.trade_ws.close()
        self.rest.close()
