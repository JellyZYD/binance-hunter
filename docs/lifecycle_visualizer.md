# Lifecycle Signal Visualizer

Use this local tool to inspect the current production lifecycle strategy on historical replay rows.

It loads:

- `backend/storage/ml/dense_lifecycle/dense_15m.parquet`
- `backend/pump_dump_hunter/ml/models/`
- `backend/config/settings.json`
- optional candles from `backend/storage/hunter_bb_300_v2.db`

It replays the current `lifecycle_router_expert` router and experts, then writes one HTML file with candlesticks, route states, internal distribution warnings, top alerts, and short alerts.

```bash
PYTHONPATH=backend python backend/ml_experiments/visualize_lifecycle_signals.py --limit 10
```

Output:

```text
backend/storage/ml/lifecycle_visualizations/lifecycle_signals.html
```

Open directly in the default browser:

```bash
PYTHONPATH=backend python backend/ml_experiments/visualize_lifecycle_signals.py --limit 10 --open
```

Serve as a local web page:

```bash
PYTHONPATH=backend python backend/ml_experiments/visualize_lifecycle_signals.py --limit 10 --serve --port 8899
```

Then open:

```text
http://127.0.0.1:8899/lifecycle_signals.html
```

Useful filters:

```bash
PYTHONPATH=backend python backend/ml_experiments/visualize_lifecycle_signals.py --symbol TLMUSDT --limit 5
PYTHONPATH=backend python backend/ml_experiments/visualize_lifecycle_signals.py --family fast_dump --level short_signal --limit 10
PYTHONPATH=backend python backend/ml_experiments/visualize_lifecycle_signals.py --life-id SYMBOL-ENTRYTIME --include-no-signal
```

Notes:

- `distribution_warning` is shown as an internal lifecycle state. The production server does not push it by default.
- The lifecycle entry marker is the replay lifecycle start / watch entry. It is not always a formal long push.
- If local 1m candles are unavailable for a selected lifecycle, the tool falls back to a synthetic dense close chart and marks the candle source accordingly.
