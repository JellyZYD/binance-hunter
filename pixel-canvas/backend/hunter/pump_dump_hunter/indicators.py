from __future__ import annotations

from .models import Candle


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def pct_change(start: float, end: float) -> float:
    return (end / start - 1.0) * 100.0 if start else 0.0


def window_metrics(candles: list[Candle], bars: int) -> tuple[float, float, float]:
    if len(candles) < bars:
        return 0.0, 0.0, 0.0
    win = candles[-bars:]
    qv = sum(c.quote_volume for c in win)
    pct = pct_change(win[0].open, win[-1].close)
    low = min(c.low for c in win)
    high = max(c.high for c in win)
    amp = pct_change(low, high)
    return qv, pct, amp


def volume_ratio(candles: list[Candle], bars: int) -> float:
    if len(candles) < bars * 2:
        return 0.0
    curr = sum(c.quote_volume for c in candles[-bars:])
    prev = sum(c.quote_volume for c in candles[-bars * 2 : -bars])
    return safe_div(curr, prev, 0.0)
