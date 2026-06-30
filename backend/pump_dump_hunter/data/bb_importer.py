from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Candle
from ..timeutils import interval_to_ms


DEFAULT_NON_ALT_SYMBOLS = {
    "AAPLUSDT",
    "AMDUSDT",
    "AMZNUSDT",
    "CLUSDT",
    "COINUSDT",
    "CRCLUSDT",
    "EWYUSDT",
    "GOOGUSDT",
    "INTCUSDT",
    "METAUSDT",
    "MSTRUSDT",
    "MSFTUSDT",
    "MUUSDT",
    "NATGASUSDT",
    "NFLXUSDT",
    "NVDAUSDT",
    "PAXGUSDT",
    "QQQUSDT",
    "SNDKUSDT",
    "SPXUSDT",
    "SPYUSDT",
    "TSLAUSDT",
    "XAGUSDT",
    "XAUUSDT",
    "XAUTUSDT",
}


@dataclass
class BbImportSummary:
    source: str
    days: int
    symbols: list[str]
    start_ms: int
    end_ms: int
    saved_rows: int
    skipped: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "days": self.days,
            "symbols": self.symbols,
            "symbol_count": len(self.symbols),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "saved_rows": self.saved_rows,
            "skipped": self.skipped,
        }


def import_bb_klines(
    store: Any,
    settings: dict[str, Any],
    source: str | Path,
    days: int,
    max_symbols: int,
    symbols: list[str] | None = None,
    rank_window_days: int = 7,
) -> BbImportSummary:
    pd, pq = require_parquet_deps()
    source = Path(source)
    klines_dir = source / "klines"
    if not klines_dir.exists():
        raise FileNotFoundError(f"bb klines directory not found: {klines_dir}")

    candidates = normalize_symbols(symbols) if symbols else intrabar_ready_symbols(pd, source, settings)
    bounds = {}
    skipped: list[str] = []
    for symbol in candidates:
        path = klines_dir / f"{symbol}.parquet"
        if not path.exists():
            skipped.append(f"{symbol}:missing")
            continue
        try:
            lo, hi = parquet_timestamp_bounds(pq, path)
        except Exception as exc:
            skipped.append(f"{symbol}:bounds:{type(exc).__name__}")
            continue
        if lo is None or hi is None:
            skipped.append(f"{symbol}:empty")
            continue
        bounds[symbol] = (lo, hi)

    if not bounds:
        raise RuntimeError("no usable bb kline parquet files")

    ranked = list(bounds.keys())
    if not symbols:
        ranked = rank_by_recent_quote_volume(pd, pq, klines_dir, ranked, bounds, rank_window_days)
    selected = ranked[: int(max_symbols)]
    end_ms = min(bounds[s][1] for s in selected)
    start_ms = end_ms - int(days) * 86_400_000 + 1
    saved = 0

    for symbol in selected:
        path = klines_dir / f"{symbol}.parquet"
        try:
            df = read_klines_slice(pd, pq, path, start_ms, end_ms)
        except Exception as exc:
            skipped.append(f"{symbol}:read:{type(exc).__name__}")
            continue
        if df.empty:
            skipped.append(f"{symbol}:no_rows_in_window")
            continue
        saved += save_dataframe_as_candles(store, symbol, df)

    return BbImportSummary(
        source=str(source),
        days=int(days),
        symbols=selected,
        start_ms=start_ms,
        end_ms=end_ms,
        saved_rows=saved,
        skipped=skipped,
    )


def require_parquet_deps():
    try:
        import pandas as pd
        import pyarrow.parquet as pq
    except Exception as exc:  # pragma: no cover - environment guard
        raise RuntimeError("pandas and pyarrow are required to import bb parquet data") from exc
    return pd, pq


def normalize_symbols(symbols: list[str] | None) -> list[str]:
    out = []
    for symbol in symbols or []:
        value = symbol.strip().upper()
        if value and value.isascii() and value.isalnum():
            out.append(value)
    return sorted(set(out))


def intrabar_ready_symbols(pd: Any, source: Path, settings: dict[str, Any]) -> list[str]:
    readiness = source / "catalog" / "symbol_readiness.csv"
    configured = settings.get("bb_import", {}).get("exclude_symbols", [])
    excluded = {str(s).upper() for s in settings["universe"].get("exclude_symbols", [])}
    excluded |= DEFAULT_NON_ALT_SYMBOLS
    excluded |= {str(s).upper() for s in configured}
    if readiness.exists():
        df = pd.read_csv(readiness)
        df = df[df["intrabar_ready"].astype(str).str.lower().eq("true")]
        symbols = [str(s).upper() for s in df["symbol"].tolist()]
    else:
        symbols = [p.stem.upper() for p in (source / "klines").glob("*.parquet")]
    return [
        s
        for s in symbols
        if s not in excluded and s.isascii() and s.isalnum() and not s.endswith(".BAK")
    ]


def parquet_timestamp_bounds(pq: Any, path: Path) -> tuple[int | None, int | None]:
    pf = pq.ParquetFile(path)
    schema_names = pf.schema_arrow.names
    if "timestamp" not in schema_names:
        return None, None
    ts_index = schema_names.index("timestamp")
    mins = []
    maxs = []
    for i in range(pf.metadata.num_row_groups):
        stats = pf.metadata.row_group(i).column(ts_index).statistics
        if stats and stats.has_min_max:
            mins.append(int(stats.min))
            maxs.append(int(stats.max))
    if mins and maxs:
        return min(mins), max(maxs)
    table = pq.read_table(path, columns=["timestamp"])
    values = table.column("timestamp").to_pylist()
    return (min(values), max(values)) if values else (None, None)


def rank_by_recent_quote_volume(
    pd: Any,
    pq: Any,
    klines_dir: Path,
    symbols: list[str],
    bounds: dict[str, tuple[int, int]],
    rank_window_days: int,
) -> list[str]:
    scores = []
    for symbol in symbols:
        path = klines_dir / f"{symbol}.parquet"
        end_ms = bounds[symbol][1]
        start_ms = end_ms - int(rank_window_days) * 86_400_000 + 1
        try:
            table = pq.read_table(
                path,
                columns=["timestamp", "quote_volume"],
                filters=[("timestamp", ">=", start_ms), ("timestamp", "<=", end_ms)],
            )
            df = table.to_pandas()
            score = float(df["quote_volume"].sum()) if not df.empty else 0.0
        except Exception:
            score = 0.0
        scores.append((score, symbol))
    scores.sort(reverse=True)
    return [symbol for _score, symbol in scores]


def read_klines_slice(pd: Any, pq: Any, path: Path, start_ms: int, end_ms: int):
    columns = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
        "taker_buy_volume",
        "taker_buy_quote_volume",
    ]
    table = pq.read_table(
        path,
        columns=columns,
        filters=[("timestamp", ">=", int(start_ms)), ("timestamp", "<=", int(end_ms))],
    )
    df = table.to_pandas()
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    return df


def save_dataframe_as_candles(store: Any, symbol: str, df: Any, chunk_size: int = 5000) -> int:
    step = interval_to_ms("1m")
    total = 0
    rows = []
    for row in df.itertuples(index=False):
        open_time = int(row.timestamp)
        rows.append(
            Candle(
                symbol=symbol,
                interval="1m",
                open_time=open_time,
                close_time=open_time + step - 1,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                quote_volume=float(row.quote_volume),
                trades=int(row.trade_count),
                taker_buy_base=float(row.taker_buy_volume),
                taker_buy_quote=float(row.taker_buy_quote_volume),
            )
        )
        if len(rows) >= chunk_size:
            total += store.save_candles(rows)
            rows = []
    if rows:
        total += store.save_candles(rows)
    return total
