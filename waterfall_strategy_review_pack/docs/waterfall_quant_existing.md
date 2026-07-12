# Waterfall Quant Research Mode

This mode is separate from the current lifecycle strategy. The default server
setting remains `runtime.active_strategy = "lifecycle"` until we explicitly
switch it.

## Objective

Find fast tradable short waterfalls across liquid altcoin USD-M futures, not
only post-pump gainers. The research path is:

1. Download a focused slice of Binance Vision aggTrade data.
2. Replay aggTrades as if the server received them live.
3. Use closed historical 1m candles only for pre-window context.
4. Let partial current 1m candles trigger faster entries.
5. Simulate paper short entries, stop loss, trailing profit, rebound exit and
   timeout exit on aggregate trade prices.

## Current Variants

- `core`: post-pump, downtrend continuation, momentum dump and other tradable
  waterfall families.
- `high_pf`: downtrend continuation and other only. This is cleaner in the 1m
  close backtest, but still needs aggTrade-specific filtering before live use.

Range breakdown is intentionally not enabled. Previous 1m tests showed it has
many false positives without order-flow or order-book confirmation.

## Commands

Download aggTrade daily zips:

```bash
PYTHONPATH=backend python backend/ml_experiments/download_binance_vision_aggtrades.py \
  --start 2026-06-10 --end 2026-06-13 --max-symbols 60 --workers 16 \
  --out-dir backend/storage/aggtrades/binance_vision
```

Replay aggTrades:

```bash
PYTHONPATH=backend python backend/ml_experiments/replay_aggtrade_waterfall.py \
  --start 2026-06-10 --end 2026-06-13 --max-symbols 60 --variant core --eval-ms 1000
```

Compare the original closed-1m replay against aggTrade partial-candle replay
on the same symbols and dates:

```bash
PYTHONPATH=backend python backend/ml_experiments/compare_waterfall_replay_modes.py \
  --start 2026-06-10 --end 2026-06-13 --max-symbols 60 --variant core \
  --families post_pump,downtrend_continuation,momentum_dump --workers 8
```

Run paper waterfall monitor:

```bash
PYTHONPATH=backend python backend/run.py waterfall-monitor --broad-top 450 --discover-every 15m
```

Switch `python run.py monitor` to this mode only after validation:

```json
{
  "runtime": {
    "active_strategy": "waterfall_quant"
  }
}
```

## Web

The page `/waterfall` shows:

- current waterfall config;
- paper open positions;
- waterfall open-short / take-profit / stop-loss / timeout signals;
- active waterfall watch pool;
- latest aggTrade replay metrics and same-window `1m close` vs `aggTrade fast`
  comparison metrics.

## Current Same-Window Replay Finding

Window: 2026-06-10 to 2026-06-13, 60 symbols, same rule set and symbols.

| Variant | Mode | Trades/day | Win | PF | Avg PnL | Avg MAE | Avg MFE | 3%+ |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| core no-other | closed 1m | 1.50 | 50.0% | 3.83 | +2.43% | 1.96% | 4.27% | 16.7% |
| core no-other | aggTrade fast | 7.00 | 42.9% | 0.69 | -0.41% | 1.72% | 2.03% | 3.6% |
| core all families | closed 1m | 2.00 | 37.5% | 2.11 | +1.30% | 2.13% | 3.68% | 12.5% |
| core all families | aggTrade fast | 8.25 | 36.4% | 0.56 | -0.61% | 1.72% | 1.81% | 3.0% |

Conclusion: directly replacing closed-1m confirmation with partial aggTrade
entry is not deployable yet. It is faster and higher-frequency, but currently
too noisy. The next research step is an aggTrade/bookTicker-specific entry
filter, not a direct period swap.

## Notes

REST aggTrades are rate-heavy and only suitable for recent short windows.
Binance Vision daily zip files are the preferred source for historical replay.
bookTicker has to be collected live if we later need full order-book-top replay.

## Execution Safety

`waterfall_quant.execution_mode` defaults to `paper`, and
`real_order_enabled` defaults to `false`. A real Binance order adapter is
reserved in code but intentionally not implemented. If the config is changed to
`live`, the monitor fails loudly instead of placing unverified real orders.
