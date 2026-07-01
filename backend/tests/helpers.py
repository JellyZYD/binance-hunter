from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from pump_dump_hunter.config import load_settings
from pump_dump_hunter.models import Candle


def temp_settings():
    settings = copy.deepcopy(load_settings())
    root = Path(tempfile.mkdtemp(prefix="pump_dump_hunter_test_"))
    settings["paths"]["db_path"] = str(root / "storage" / "hunter.db")
    settings["paths"]["alerts_dir"] = str(root / "alerts")
    settings["paths"]["cache_dir"] = str(root / "cache")
    settings["paths"]["reports_dir"] = str(root / "reports")
    settings["backtest"]["min_signals"] = 1
    settings["signals"]["mode"] = "legacy"  # 这些用例针对 legacy 信号行为, 与生产默认(ml)解耦
    settings["_tmp_root"] = str(root)
    return settings


def candle(symbol: str, interval: str, open_time: int, open_: float, close: float, qv: float = 1000.0) -> Candle:
    high = max(open_, close) * 1.002
    low = min(open_, close) * 0.998
    step = 60_000 if interval == "1m" else 300_000
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=open_time + step - 1,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        quote_volume=qv,
        trades=100,
        taker_buy_base=50.0,
        taker_buy_quote=qv / 2.0,
    )


def pump_dump_1m(symbol: str = "PUMPUSDT", start: int = 1_700_000_100_000) -> list[Candle]:
    rows = []
    price = 100.0
    for i in range(70):
        rows.append(candle(symbol, "1m", start + i * 60_000, price, price, 1000.0))
    for i in range(30):
        nxt = price + (50.0 / 30.0)
        rows.append(candle(symbol, "1m", start + (70 + i) * 60_000, price, nxt, 8000.0))
        price = nxt
    for i in range(15):
        rows.append(candle(symbol, "1m", start + (100 + i) * 60_000, price, price * 0.999, 1200.0))
        price *= 0.999
    dump_prices = [146.0, 144.0, 142.0, 141.0, 140.0]
    for j, nxt in enumerate(dump_prices):
        rows.append(candle(symbol, "1m", start + (115 + j) * 60_000, price, nxt, 25000.0))
        price = nxt
    for i in range(20):
        nxt = price * 0.998
        rows.append(candle(symbol, "1m", start + (120 + i) * 60_000, price, nxt, 25000.0))
        price = nxt
    return rows
