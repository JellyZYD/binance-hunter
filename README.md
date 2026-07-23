# Binance Hunter

Binance USD-M perpetual waterfall monitor, paper-trading research system and
fail-closed live-execution workbench. The checked-in base configuration remains
paper-only; the live module is disabled by default and cannot place an
order without explicit configuration, confirmation, notional cap and one-time nonce.

## Current production runtime

`backend/config/settings.json` sets:

```json
{
  "runtime": { "active_strategy": "claude_board_wf_1m" }
}
```

Production runs one signal engine and three independent paper ledgers:

| Signal engine | Strategy id | Entry |
| --- | --- | --- |
| Board Waterfall | `claude_board_wf_1m` | 24h gain >= 40%, 60m drawdown >= 7%, 60m quote volume >= 300k USDT |

The retired Codex core5 engine is disabled. Its historical SQLite rows are kept
for audit but are not loaded, displayed or allowed to generate signals.

The three Claude ledgers all start at 100 USDT and replay the same master trades
from 2026-07-13 07:37 CST: 20% fixed margin, 10% fixed margin, and 10% base
margin with a realized-equity drawdown ladder. Signals and WeCom notifications
are emitted once; each notification contains all three account changes.

Real execution remains disabled by `live_trading.enabled=false` and
`live_trading.real_order_enabled=false`. A private server config enables it
without committing credentials or live authorization.

The verified Binance account is a Portfolio Margin unified account in hedge
mode. Private account/order calls use `https://papi.binance.com`; public USD-M
market data still uses `https://fapi.binance.com`. On 2026-07-18, local
mainnet micro tests passed for account reads, the PM user-data stream, MARKET
open/close, IOC LIMIT, GTX post-only place/query/cancel, and STOP/TAKE_PROFIT
Algo orders. The account was left with zero positions and zero open orders.
Checked-in configuration is still fail-closed.

Every real order is now audited from strategy decision through exchange fill.
The isolated live ledger records decision-to-submit, submit-to-ACK,
submit-to-first-fill and decision-to-final-fill latency, plus both total
signal-price slippage and order-arrival slippage. Positive slippage is adverse;
negative slippage is price improvement. These fields are exposed only when the
disabled-by-default live dashboard is explicitly enabled.

The live sizing path uses 20% base margin, 5x exchange leverage, and
realized-equity drawdown factors
`1.0/0.75/0.50/0.25` at 5%/10%/15% drawdown. Deposits, withdrawals and
transfers do not change strategy equity or its drawdown tier. At most three live
positions, an explicit notional cap and exchange depth remain hard operational
limits.

The server paper monitor is the sole public K-line and strategy engine. It
publishes ordered Claude signals and exact trailing-protection state to the
shared `hunter.db`; live execution tails that durable outbox at 100ms intervals.
It does not run a second 400-symbol WebSocket or independently reconstruct
signals, eliminating paper/live missing-candle divergence.

The live reconciliation path is self-healing for connectivity failures. Account,
position, ordinary-order and Algo-order state form the authoritative critical
snapshot; time sync and income history are optional catch-up tasks. Read-only
REST calls reuse persistent HTTPS sessions and retry with exponential backoff
and jitter. A complete later snapshot automatically clears only recovered
connectivity halts. A later exchange snapshot may also clear protection/cancel
halts only when the expected stop exists or the timed-out order is proven gone.
External positions/orders, margin risk, daily loss and unknown executions remain
fail-closed.

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend/` | Python strategy engines, REST/WebSocket data, SQLite, replay and read-only API |
| `frontend/` | Next.js waterfall dashboard |
| `database/` | SQLite schema reference and storage notes |
| `deploy/` | Ubuntu, systemd, Nginx and update scripts |
| `docs/` | Production strategy, collector, frontend and historical research documentation |
| `waterfall_strategy_review_pack/` | Offline review evidence and research artifacts; not imported by production |

The live implementation is isolated under `backend/pump_dump_hunter/live_trading/`.
See [`docs/champion/03-实盘交易系统实施方案.md`](docs/champion/03-实盘交易系统实施方案.md).

## Local startup

Backend API:

```powershell
cd E:\A\pixel-canvas-v2\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py web --host 127.0.0.1 --port 8787
```

Monitor:

```powershell
cd E:\A\pixel-canvas-v2\backend
python run.py monitor
```

Frontend:

```powershell
cd E:\A\pixel-canvas-v2\frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Open `http://localhost:3000`.

## Reproducible Board replay

The replay imports the production `BoardWaterfallEngine` and merges every
selected symbol by candle close time. Account equity, free margin, maximum open
positions, cooldowns, fees and slippage therefore follow the live paper path.

```powershell
python backend/ml_experiments/backtest_board_waterfall.py `
  --klines-dir "E:\A\bb\data\klines" `
  --start 2026-01-01 --end 2026-06-30 `
  --split-date 2026-04-01
```

The output contains a trade CSV and JSON metrics for all/train/holdout periods,
including frequency, win rate, PF, average/median return, MAE/MFE, 3%/5% winner
rates and PF after removing the largest winner.

## Verification

```powershell
cd E:\A\pixel-canvas-v2\backend
python -m pytest tests -q

cd ..\frontend
npm run build
```

Current local verification: 146 backend tests pass and the Next.js production
build succeeds. This proves deterministic state-machine behavior under the
covered fault injections; it does not replace the required 7-14 day / 30-trade
mainnet micro validation before any larger capital is enabled.

Production verification after deployment:

```bash
cd /opt/binance-hunter
python3 deploy/verify-live.py
```

The verifier requires the Claude 1m champion runtime, disabled core5 execution,
paper-only execution, all three independent accounts and a combined 300 USDT
initial balance.

## Deployment and update

Initial deployment is documented in [`deploy/README.md`](deploy/README.md).
Update an existing server with:

```bash
cd /opt/binance-hunter
sudo bash deploy/update.sh
```

The monitor uses DB-first prewarm. If Binance universe REST is unavailable or
rate-limited, it falls back to known SQLite symbols in strict DB-only mode and
does not continue per-symbol kline REST calls during that fallback.

## Documentation map

- [`docs/claude-paper-accounts.md`](docs/claude-paper-accounts.md): three-account sizing, replay and notification design.
- [`docs/waterfall_quant.md`](docs/waterfall_quant.md): retired core5 + agg strategy kept for research history.
- [`docs/board_waterfall.md`](docs/board_waterfall.md): Board Waterfall strategy and replay.
- [`docs/micro-collector.md`](docs/micro-collector.md): aggTrade, book/depth and OI collection.
- [`docs/frontend.md`](docs/frontend.md): dashboard routes and API proxy.
- [`docs/production-fix-20260713.md`](docs/production-fix-20260713.md): dual-engine recovery fix and release checks.
- [`deploy/README.md`](deploy/README.md): server operations and resource limits.
