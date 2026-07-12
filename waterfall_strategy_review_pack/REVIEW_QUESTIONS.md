# Review Questions For Another AI

Please review this strategy as a quant research problem, not as production-ready code.

## Main Question

Can the current best candidate, `hybrid_micro_sell60`, be improved while preserving its advantage over the closed 1m baseline?

Current best candidate:

```text
closed 1m waterfall structure signal
+ signal-minute aggTrade sell_ratio >= 0.60
+ aggTrade fast stop / quick reclaim stop
+ 1m profit-holding exit logic
```

## What Looks Promising

- Requiring signal-minute taker sell pressure improves quality.
- Late-minute lows (`low_sec` high) distinguish real continuation from early wick-and-rebound.
- AggTrade is useful for fast stop/quick reclaim.
- AggTrade full trailing exit is bad because it cuts big dumps too early.

## What Needs Skeptical Review

1. Is `sell_ratio >= 0.60` robust, or is it overfit to the available June 2026 sample?
2. Should the threshold be dynamic by symbol liquidity, recent volatility, or family?
3. Should `low_sec` be used as a strong-signal tier only, or as a required main filter?
4. Are post-pump, downtrend-continuation, momentum-dump, and other families too broad?
5. Can the exit be improved without cutting large waterfalls?
6. Can we increase trades/day without degrading PF and MAE?
7. Is there a better way to model wick traps using only aggTrade history?
8. Should corrupted/missing aggTrade data invalidate any part of the current conclusion?
9. What is the best train/validation split for threshold search?
10. How should bookTicker be collected live so future versions can be honestly replayed?

## Acceptance Criteria Before Deployment

Do not deploy unless a candidate beats the closed 1m baseline on a held-out sample:

- higher trades/day
- higher win rate
- higher profit factor
- higher average PnL
- lower or equal MAE
- no large degradation in big-dump capture rate

If a candidate improves PF but lowers frequency too much, classify it as a strong-signal tier rather than the main strategy.

## Known Failed Directions

- Raw aggTrade early entry.
- Preclose entry at 45s or 55s as main strategy.
- Full tick-level trailing exit for winners.
- Tight early adverse stops at 0.8%, 1.2%, 1.6%.

## Suggested Review Workflow

1. Start with `README.md`.
2. Inspect latest result JSON:
   - `results/waterfall_mode_compare_metrics_20260711_211535.json`
   - `results/agg_entry_signal_filter_report_20260711_210033.json`
3. Inspect code:
   - `code/compare_waterfall_replay_modes.py`
   - `code/analyze_agg_entry_signal_filters.py`
   - `code/waterfall.py`
4. Recommend one of:
   - keep `micro_sell60` and validate wider
   - replace the threshold logic
   - redesign exits
   - collect more data first
   - abandon aggTrade-only improvement and require bookTicker/order-book data
