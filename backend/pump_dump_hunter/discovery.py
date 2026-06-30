from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .data.rest_client import BinanceRestClient
from .indicators import volume_ratio, window_metrics
from .models import Candle, LiquidityRecord, SignalParams
from .timeutils import closed_candle_cutoff_ms


def perpetual_symbols(exchange_info: dict[str, Any], settings: dict[str, Any]) -> set[str]:
    uni = settings["universe"]
    excluded = set(uni.get("exclude_symbols", []))
    out = set()
    for item in exchange_info.get("symbols", []):
        symbol = str(item.get("symbol", ""))
        if not symbol.isascii() or not symbol.isalnum():
            continue
        if (
            item.get("contractType") == uni["contract_type"]
            and item.get("status") == "TRADING"
            and item.get("quoteAsset") == uni["quote_asset"]
            and symbol not in excluded
        ):
            out.add(symbol)
    return out


def build_broad_universe(client: BinanceRestClient, settings: dict[str, Any], broad_top: int | None = None) -> list[dict[str, Any]]:
    exchange = client.exchange_info()
    tickers = client.ticker_24h_all()
    perps = perpetual_symbols(exchange, settings)
    min_qv = float(settings["universe"].get("min_24h_quote_volume", 0.0))
    rows = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if symbol not in perps:
            continue
        qv = float(ticker.get("quoteVolume", 0.0) or 0.0)
        if qv < min_qv:
            continue
        rows.append(
            {
                "symbol": symbol,
                "last_price": float(ticker.get("lastPrice", 0.0) or 0.0),
                "quote_volume_24h": qv,
                "pct_24h": float(ticker.get("priceChangePercent", 0.0) or 0.0),
            }
        )
    rows.sort(key=lambda r: r["quote_volume_24h"], reverse=True)
    return rows[: int(broad_top or settings["universe"]["broad_top"])]


def discover_recent_liquidity(
    client: BinanceRestClient,
    settings: dict[str, Any],
    top_n: int | None = None,
    broad_top: int | None = None,
    now_ms: int | None = None,
    params: SignalParams | None = None,
    max_workers: int = 12,
) -> tuple[list[LiquidityRecord], dict[str, Any]]:
    params = params or SignalParams.from_dict(settings["params"])
    top_n = int(top_n or settings["universe"]["default_top_n"])
    broad = build_broad_universe(client, settings, broad_top=broad_top)
    reference_time = now_ms or max(t.get("closeTime", 0) for t in client.ticker_24h_all())
    cutoff = closed_candle_cutoff_ms(reference_time, "1m")
    limit = int(settings["discovery"]["recent_limit"])
    context_interval = str(settings["discovery"].get("context_interval", "15m"))
    context_limit = int(settings["discovery"].get("context_limit", 97))
    context_cutoff = closed_candle_cutoff_ms(reference_time, context_interval)
    candles_by_symbol: dict[str, list[Candle]] = {}
    context_by_symbol: dict[str, list[Candle]] = {}
    errors: list[dict[str, str]] = []

    def fetch(row: dict[str, Any]) -> tuple[str, list[Candle], list[Candle]]:
        symbol = row["symbol"]
        candles = client.klines(symbol, "1m", limit=limit)
        context = client.klines(symbol, context_interval, limit=context_limit)
        closed = [c for c in candles if c.close_time <= cutoff]
        context_closed = [c for c in context if c.close_time <= context_cutoff]
        return symbol, closed, context_closed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch, row): row for row in broad}
        for fut in as_completed(futs):
            row = futs[fut]
            try:
                symbol, candles, context = fut.result()
                candles_by_symbol[symbol] = candles
                context_by_symbol[symbol] = context
            except Exception as exc:
                errors.append({"symbol": row["symbol"], "error": f"{type(exc).__name__}: {exc}"[:160]})

    records = compute_liquidity_records(
        broad,
        candles_by_symbol,
        top_n=top_n,
        data_cutoff_time=cutoff,
        params=params,
        context_candles_by_symbol=context_by_symbol,
    )
    meta = {
        "broad_count": len(broad),
        "selected_count": len([r for r in records if r.selected]),
        "pump_count": len([r for r in records if r.pump_qualified]),
        "data_cutoff_time": cutoff,
        "errors": errors,
    }
    return records, meta


def compute_liquidity_records(
    broad_rows: list[dict[str, Any]],
    candles_by_symbol: dict[str, list[Candle]],
    top_n: int,
    data_cutoff_time: int,
    params: SignalParams,
    context_candles_by_symbol: dict[str, list[Candle]] | None = None,
) -> list[LiquidityRecord]:
    context_candles_by_symbol = context_candles_by_symbol or {}
    temp = []
    for row in broad_rows:
        symbol = row["symbol"]
        candles = [c for c in candles_by_symbol.get(symbol, []) if c.close_time <= data_cutoff_time]
        if len(candles) < 31:
            continue
        qv15, pct15, amp15 = window_metrics(candles, 15)
        qv30, pct30, amp30 = window_metrics(candles, 30)
        context = context_candles_by_symbol.get(symbol, [])
        qv4h, pct4h, _amp4h = window_metrics(context, 16)
        qv12h, pct12h, _amp12h = window_metrics(context, 48)
        qv1d, pct1d, _amp1d = window_metrics(context, 96)
        if not context:
            pct1d = float(row.get("pct_24h", 0.0) or 0.0)
        temp.append(
            {
                "symbol": symbol,
                "last_price": candles[-1].close,
                "quote_volume_15m": qv15,
                "quote_volume_30m": qv30,
                "pct_15m": pct15,
                "pct_30m": pct30,
                "amp_15m": amp15,
                "amp_30m": amp30,
                "volume_ratio_15m": volume_ratio(candles, 15),
                "volume_ratio_30m": volume_ratio(candles, 30),
                "pct_24h": float(row.get("pct_24h", 0.0) or 0.0),
                "quote_volume_4h": qv4h,
                "quote_volume_12h": qv12h,
                "quote_volume_1d": qv1d,
                "pct_4h": pct4h,
                "pct_12h": pct12h,
                "pct_1d": pct1d,
            }
        )
    gain15 = {r["symbol"]: i + 1 for i, r in enumerate(sorted(temp, key=lambda r: r["pct_15m"], reverse=True))}
    gain30 = {r["symbol"]: i + 1 for i, r in enumerate(sorted(temp, key=lambda r: r["pct_30m"], reverse=True))}
    temp.sort(key=lambda r: r["quote_volume_15m"] * 2.0 + r["quote_volume_30m"], reverse=True)

    out: list[LiquidityRecord] = []
    for i, row in enumerate(temp, 1):
        selected = i <= top_n
        pump = selected and is_pump_qualified(row, gain15[row["symbol"]], gain30[row["symbol"]], params)
        out.append(
            LiquidityRecord(
                symbol=row["symbol"],
                rank=i,
                last_price=row["last_price"],
                quote_volume_15m=row["quote_volume_15m"],
                quote_volume_30m=row["quote_volume_30m"],
                pct_15m=row["pct_15m"],
                pct_30m=row["pct_30m"],
                amp_15m=row["amp_15m"],
                amp_30m=row["amp_30m"],
                volume_ratio_15m=row["volume_ratio_15m"],
                volume_ratio_30m=row["volume_ratio_30m"],
                gain_rank_15m=gain15[row["symbol"]],
                gain_rank_30m=gain30[row["symbol"]],
                selected=selected,
                pump_qualified=pump,
                data_cutoff_time=data_cutoff_time,
                pct_4h=row["pct_4h"],
                pct_12h=row["pct_12h"],
                pct_1d=row["pct_1d"],
                quote_volume_4h=row["quote_volume_4h"],
                quote_volume_12h=row["quote_volume_12h"],
                quote_volume_1d=row["quote_volume_1d"],
            )
        )
    return out


def is_pump_qualified(row: dict[str, Any], gain_rank_15m: int, gain_rank_30m: int, params: SignalParams) -> bool:
    if float(row.get("pct_24h", 0.0)) > params.max_24h_pct:
        return False
    direct = (
        row["pct_15m"] >= params.pump_15m_pct
        or row["pct_30m"] >= params.pump_30m_pct
        or row.get("pct_4h", 0.0) >= params.pump_4h_pct
        or row.get("pct_12h", 0.0) >= params.pump_12h_pct
        or row.get("pct_1d", 0.0) >= params.pump_1d_pct
    )
    ranked_15m = (
        gain_rank_15m <= params.gain_rank_top
        and row["pct_15m"] >= params.ranked_min_15m_pct
        and row["volume_ratio_15m"] >= params.volume_ratio_15m
    )
    ranked_30m = (
        gain_rank_30m <= params.gain_rank_top
        and row["pct_30m"] >= params.ranked_min_30m_pct
        and row["volume_ratio_30m"] >= params.volume_ratio_30m
    )
    return bool(direct or ranked_15m or ranked_30m)
