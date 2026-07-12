# Frontend Source

This Next.js app is display-only. Strategy state and paper trading data come
from the Python backend.

## Entry Points

- `app/page.tsx`: renders `WaterfallDashboard`.
- `app/waterfall/page.tsx`: renders `WaterfallDashboard`.
- `components/hunter/WaterfallDashboard.tsx`: current production waterfall
  quant interface.
- `components/hunter/HunterDashboard.tsx`: legacy lifecycle interface, retained
  for manual inspection only.
- `app/api/hunter/[...path]/route.ts`: proxy to Python backend API.
- `app/globals.css`: plain CSS styles.

## Data Flow

```text
Browser -> Next /api/hunter/* -> Python backend /api/* -> SQLite
```

The browser never reads SQLite directly and never writes strategy state.
