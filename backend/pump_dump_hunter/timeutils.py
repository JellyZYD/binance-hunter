from __future__ import annotations

from datetime import datetime, timezone


def utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_from_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def local_day_from_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def local_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def interval_to_ms(interval: str) -> int:
    interval = interval.strip().lower()
    unit = interval[-1]
    value = int(interval[:-1])
    if unit == "m":
        return value * 60_000
    if unit == "h":
        return value * 3_600_000
    if unit == "d":
        return value * 86_400_000
    raise ValueError(f"unsupported interval: {interval}")


def parse_duration_seconds(value: str) -> int:
    value = str(value).strip().lower()
    if value.endswith("ms"):
        return max(1, int(value[:-2]) // 1000)
    if value.endswith("s"):
        return int(value[:-1])
    if value.endswith("m"):
        return int(value[:-1]) * 60
    if value.endswith("h"):
        return int(value[:-1]) * 3600
    return int(value)


def closed_candle_cutoff_ms(now_ms: int, interval: str = "1m") -> int:
    step = interval_to_ms(interval)
    return (now_ms // step) * step - 1
