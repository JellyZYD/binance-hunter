# Binance Hunter

Binance USD-M perpetual waterfall monitor and paper-trading research system.
The current production runtime detects high-quality short opportunities from
closed 1m candles and aggTrade flow. It does not place real orders.

## Current production runtime

`backend/config/settings.json` sets:

```json
{
  "runtime": { "active_strategy": "waterfall_quant" }
}
```

Two independent engines consume the same 1m WebSocket candle store:

| Engine | Strategy id | Entry | Paper account |
| --- | --- | --- | ---: |
| Codex core5 + agg | `waterfall_core5_agg_1m` | closed 1m core structure plus aggTrade sell-pressure confirmation | 100 USDT |
| Board Waterfall | `claude_board_wf_1m` | 24h gain >= 40%, 60m drawdown >= 7%, 60m quote volume >= 300k USDT | 100 USDT |

The accounts are isolated. Positions, realized PnL, cooldowns, trade counts and
exit profiles are restored by strategy id after restart. The dashboard total
starts from 200 USDT and `accounts[]` exposes both 100 USDT accounts separately.

Real execution is disabled by both `execution_mode="paper"` and
`real_order_enabled=false`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend/` | Python strategy engines, REST/WebSocket data, SQLite, replay and read-only API |
| `frontend/` | Next.js waterfall dashboard |
| `database/` | SQLite schema reference and storage notes |
| `deploy/` | Ubuntu, systemd, Nginx and update scripts |
| `docs/` | Production strategy, collector, frontend and historical research documentation |
| `waterfall_strategy_review_pack/` | Offline review evidence and research artifacts; not imported by production |

## Local startup

Backend API:

```powershell
cd E:\A\pixel-canvas\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py web --host 127.0.0.1 --port 8787
```

Monitor:

```powershell
cd E:\A\pixel-canvas\backend
python run.py monitor
```

Frontend:

```powershell
cd E:\A\pixel-canvas\frontend
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
cd E:\A\pixel-canvas\backend
python -m pytest tests -q

cd ..\frontend
npm run build
```

Production verification after deployment:

```bash
cd /opt/binance-hunter
python3 deploy/verify-live.py
```

The verifier requires the 1m waterfall runtime, core5 families, aggTrade gate,
paper-only execution, both independent accounts and a combined 200 USDT initial
balance.

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

- [`docs/waterfall_quant.md`](docs/waterfall_quant.md): core5 + agg production strategy.
- [`docs/board_waterfall.md`](docs/board_waterfall.md): Board Waterfall strategy and replay.
- [`docs/micro-collector.md`](docs/micro-collector.md): aggTrade, book/depth and OI collection.
- [`docs/frontend.md`](docs/frontend.md): dashboard routes and API proxy.
- [`docs/production-fix-20260713.md`](docs/production-fix-20260713.md): dual-engine recovery fix and release checks.
- [`deploy/README.md`](deploy/README.md): server operations and resource limits.
