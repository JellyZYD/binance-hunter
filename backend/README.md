# Binance Hunter Backend

The Python backend owns market data, both production waterfall engines, SQLite
state, paper execution, replay tools and the read-only dashboard API. It runs
without Next.js.

## Production modules

| Path | Responsibility |
| --- | --- |
| `pump_dump_hunter/waterfall.py` | Codex core5 + agg engine, shared monitor loop, REST/DB prewarm, push and paper execution |
| `pump_dump_hunter/board_waterfall.py` | Board Waterfall engine and E1 exit lifecycle |
| `pump_dump_hunter/data/store.py` | SQLite schema, strategy-scoped state and account summaries |
| `pump_dump_hunter/data/websocket_source.py` | closed 1m kline and micro-stream delivery |
| `pump_dump_hunter/discovery.py` | liquid USDT perpetual universe and exclusion filtering |
| `pump_dump_hunter/web.py` | read-only API consumed by the dashboard |
| `ml_experiments/backtest_board_waterfall.py` | exact production-engine Board replay |
| `config/settings.json` | production configuration |

The older lifecycle long/top/short modules remain for historical research but
are not the default runtime. `runtime.active_strategy` is `waterfall_quant`.

## Runtime flow

1. Refresh the liquid USDT perpetual universe through REST every 15 minutes.
2. Keep active positions subscribed even if they leave the latest TopN.
3. Consume closed 1m klines over WebSocket.
4. Feed the candle once to core5 and Board Waterfall using a shared candle map.
5. Persist changed watch rows, positions and signals before best-effort push.
6. Restore each engine by its own strategy id after restart.

If universe REST fails, prewarm switches to DB-only mode. WebSocket monitoring
continues from known symbols and no per-symbol kline REST requests are issued in
that fallback pass.

## Independent paper accounts

- `waterfall_core5_agg_1m`: 100 USDT initial equity.
- `claude_board_wf_1m`: 100 USDT initial equity.
- Default margin fraction: 20% of current equity.
- Default leverage: 10x.
- Maximum open positions: five per engine.
- New positions require positive free margin and notional.

SQLite queries and in-memory restore both enforce strategy filtering. Complete
closed-position history is loaded for realized PnL, so long-running restarts do
not lose early account results.

## Commands

```bash
python run.py monitor
python run.py waterfall-monitor --broad-top 450 --discover-every 15m
python run.py web --host 127.0.0.1 --port 8787
python run.py status --limit 20
```

Exact Board replay:

```bash
python ml_experiments/backtest_board_waterfall.py \
  --klines-dir "E:\\A\\bb\\data\\klines" \
  --start 2026-01-01 --end 2026-06-30 \
  --split-date 2026-04-01
```

## API

Relevant routes:

```text
/api/waterfall/summary
/api/waterfall/watch
/api/waterfall/positions
/api/waterfall/signals
/api/system
```

`waterfall/summary` contains a combined total plus an `accounts[]` entry for
each independent engine. The combined initial balance is 200 USDT when both
engines are enabled.

## Tests

```bash
python -m pytest tests -q
python -m py_compile \
  pump_dump_hunter/waterfall.py \
  pump_dump_hunter/board_waterfall.py \
  pump_dump_hunter/data/store.py \
  pump_dump_hunter/web.py
```

`tests/test_waterfall_dual_strategy.py` covers strategy isolation, complete
history queries, account aggregation, DB-only fallback, zero-equity sizing and
the Board entry/trailing-exit lifecycle.

## Runtime files

- `storage/hunter.db`
- `storage/monitor_health.json`
- `alerts/waterfall-YYYY-MM-DD.jsonl`
- `alerts/waterfall-YYYY-MM-DD.md`
- `storage/backtests/board_waterfall/`

Runtime storage is excluded from Git.
