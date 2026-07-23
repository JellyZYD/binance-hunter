# Frontend

The frontend is a lightweight Next.js dashboard. It only reads the Python API
and does not access SQLite directly.

## Production Pages

- `src/app/page.tsx`: production waterfall quant dashboard.
- `src/app/waterfall/page.tsx`: same waterfall dashboard route.
- `src/components/hunter/WaterfallDashboard.tsx`: account, paper positions,
  signals, trades, watch pool and replay metrics.
- `src/components/hunter/HunterDashboard.tsx`: legacy lifecycle dashboard kept
  for historical inspection, not the default page.
- `src/app/api/hunter/[...path]/route.ts`: proxy to the Python API.

## API Proxy

Browser requests use:

```text
/api/hunter/waterfall/summary
/api/hunter/waterfall/watch
/api/hunter/waterfall/positions
/api/hunter/waterfall/signals
/api/hunter/waterfall/replay-results
/api/hunter/live/summary
```

The summary response has two levels:

- top-level fields combine the three Claude paper accounts;
- `accounts[]` contains 20% fixed, 10% fixed and 10% drawdown-ladder ledgers.

Top-level `paper_initial_balance_usdt` is 300 while each account remains 100.
Positions and signals are the single Claude master path and are not triplicated.
Historical core5 database rows are excluded by default from the API and UI.

The live summary is a sanitized read-only view of the independent execution
ledger. It shows account equity, wallet/available balance, unrealized and net
realized PnL, commission, funding, realized-equity drawdown, initial margin and
notional per position/order, protection status, decision-to-fill latency, and
signal/arrival slippage. Quantity is not the primary user-facing sizing field.
API keys, secrets, webhook URLs and raw exchange payloads are never returned.
The public response also omits the service PID and the private SQLite path.

On a server with `backend/config/live.server.json`, the read-only API uses that
file only to report the effective non-secret execution mode and risk limits.
The checked-in config still keeps order execution disabled. The server builder
sets `dashboard_enabled=true` because the user explicitly requested the
sanitized live account panel.

The 2026-07-24 release pins Next.js and `eslint-config-next` to `16.2.11` and
overrides Next's bundled PostCSS/Sharp to patched versions. `npm audit` reports
zero production and development vulnerabilities; the production build passes.

Next.js forwards them to:

```text
${HUNTER_API_BASE_URL}/api/*
```

Local default:

```text
HUNTER_API_BASE_URL=http://127.0.0.1:8787
```
