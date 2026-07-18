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
```

The summary response has two levels:

- top-level fields combine the three Claude paper accounts;
- `accounts[]` contains 20% fixed, 10% fixed and 10% drawdown-ladder ledgers.

Top-level `paper_initial_balance_usdt` is 300 while each account remains 100.
Positions and signals are the single Claude master path and are not triplicated.
Historical core5 database rows are excluded by default from the API and UI.

Next.js forwards them to:

```text
${HUNTER_API_BASE_URL}/api/*
```

Local default:

```text
HUNTER_API_BASE_URL=http://127.0.0.1:8787
```
