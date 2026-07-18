# Binance Hunter Backend

The Python backend owns market data, the production Claude waterfall engine, SQLite
state, paper execution, replay tools and the read-only dashboard API. It runs
without Next.js.

## Production modules

| Path | Responsibility |
| --- | --- |
| `pump_dump_hunter/waterfall.py` | monitor loop, retired core5 research code, REST/DB prewarm, push and paper execution |
| `pump_dump_hunter/board_waterfall.py` | Board Waterfall engine and E1 exit lifecycle |
| `pump_dump_hunter/paper_accounts.py` | three independent sizing ledgers driven by one Claude signal path |
| `pump_dump_hunter/data/store.py` | SQLite schema, strategy-scoped state and account summaries |
| `pump_dump_hunter/data/websocket_source.py` | closed 1m kline and micro-stream delivery |
| `pump_dump_hunter/discovery.py` | liquid USDT perpetual universe and exclusion filtering |
| `pump_dump_hunter/web.py` | read-only API consumed by the dashboard |
| `ml_experiments/backtest_board_waterfall.py` | exact production-engine Board replay |
| `config/settings.json` | production configuration |

The older lifecycle long/top/short modules remain for historical research but
are not the default runtime. `runtime.active_strategy` is `claude_board_wf_1m`.

## Runtime flow

1. Refresh the liquid USDT perpetual universe through REST every 15 minutes.
2. Keep active positions subscribed even if they leave the latest TopN.
3. Consume closed 1m klines over WebSocket.
4. Feed each candle once to Board Waterfall; core5 is not instantiated.
5. Persist the master position, update all three account ledgers and push once.
6. Rebuild derived ledgers from Claude master history after every restart.

If universe REST fails, prewarm switches to DB-only mode. WebSocket monitoring
continues from known symbols and no per-symbol kline REST requests are issued in
that fallback pass.

## Independent paper accounts

- `claude_fixed20`: 100U, fixed 20% margin, 10x.
- `claude_fixed10`: 100U, fixed 10% margin, 10x.
- `claude_drawdown10`: 100U, 10% base margin with realized-drawdown scaling, 10x.
- Maximum master positions: five; accounts with no free equity skip the trade.

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
each independent sizing ledger. The combined initial balance is 300 USDT.

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
