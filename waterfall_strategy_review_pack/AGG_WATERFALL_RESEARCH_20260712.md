# Agg Waterfall Research Update - 2026-07-12

## Scope

Goal: improve the 1m waterfall short strategy with aggTrade microstructure data, without degrading the original 1m closed-candle strategy.

This update fixes two audit issues first:

- Corrected linear USDT short PnL from `entry / exit - 1` to `1 - exit / entry`.
- Made 1m bar exit simulation more conservative by not using the current bar low to update a trailing stop before checking whether the same bar high hit the old stop.

## Data

Core 1m waterfall trade sample:

- Source trades: `backend/storage/ml/waterfall_patterns_formula_fixed_core5/waterfall_trades_20260712_004858.csv`
- Total core trades: 1555
- Validation split: `signal_time >= 2026-04-01`
- aggTrade core download jobs: 1066 symbol-days
- Download result with short timeout:
  - downloaded: 346
  - existed: 379
  - skipped/error: 323
  - bytes: about 10.1 GB

Feature coverage:

- Train period requested: 1000 trades, covered: 633
- Validation period requested: 555 trades, covered: 401
- Combined agg feature rows: 1034

Feature files:

- Train: `backend/storage/ml/agg_waterfall_trade_features_core5_train_current/agg_waterfall_trade_features_fast_20260712_023826.csv`
- Validation: `backend/storage/ml/agg_waterfall_trade_features_core5_validation_current/agg_waterfall_trade_features_fast_20260712_021853.csv`
- Combined: `backend/storage/ml/agg_waterfall_trade_features_core5_combined_current/agg_waterfall_trade_features_combined_20260712.csv`

## Baseline After Fixes

The 1m core strategy after PnL/exit fixes, using original 0.08% round-trip cost:

- Overall: 1555 trades, 4.24/day, win 54.6%, avg +1.94%, PF 2.48
- Holdout from 2026-04: 555 trades, 6.99/day, win 52.1%, avg +0.84%, PF 1.62

With conservative 0.30% round-trip cost:

- Holdout from 2026-04: avg +0.62%, PF 1.42

In the agg-covered validation subset only:

- 401 trades, 5.01/day, win 49.9% after 0.30% cost, avg +0.52%, PF 1.35

## ML Result

LightGBM was tested as a trade-quality filter using train before 2026-04 and validation after 2026-04.

Result:

- `closed_1m` validation AUC: about 0.45-0.47
- `preclose_agg` validation AUC: about 0.47-0.51

Conclusion:

- LGB did not learn a stable trade-quality filter on this 1034-row agg feature dataset.
- The useful signal is simpler and more structural: sustained aggressive sell flow plus close near the minute low.

## Useful Agg Filters

All returns below use conservative 0.30% round-trip cost.

### Strong Agg Quality Filter

Condition:

```text
m0_50s_sell_ratio >= 0.64
and m0_50s_close_pos <= 0.15
```

Meaning:

- By the 50th second of the signal minute, at least 64% of quote volume is aggressive sell.
- Price is closing near the low of the current minute range.

Train:

- 130 trades, 0.49/day
- Win 53.8%
- Avg +4.79%
- PF 4.16
- Median MAE 2.31%

Validation:

- 48 trades, 0.62/day
- Win 60.4%
- Avg +2.29%
- Median +2.22%
- PF 3.00
- Median MAE 1.33%
- 3%+ rate 31.3%
- 5%+ rate 25.0%

Use:

- High-confidence strong signal.
- Frequency is too low to be the only strategy.

### High-Quality Family Combo

Condition:

```text
downtrend_continuation:
  m0_40s_sell_ratio >= 0.64
  and m0_59s_low_time_frac >= 0.8

other:
  no extra agg filter
```

Validation:

- 70 trades, 0.92/day
- Win 65.7%
- Avg +2.05%
- PF 3.04
- Median MAE 1.72%

Use:

- Best quality profile, but still below 2/day.

### Higher-Frequency Family Combo

Condition:

```text
downtrend_continuation:
  m0_40s_sell_ratio >= 0.64
  and m0_59s_low_time_frac >= 0.8

other:
  no extra agg filter

post_pump:
  no extra agg filter
```

Validation:

- 196 trades, 2.48/day
- Win 57.7%
- Avg +1.16%
- Median +2.17%
- PF 1.93
- Median MAE 1.92%
- 3%+ rate 30.1%
- 5%+ rate 11.7%

Use:

- Best candidate if the requirement is at least 2 trades/day.
- PF is lower than the strong filter but still materially better than the agg-covered baseline PF 1.35 after 0.30% cost.

### Higher-Frequency Plus Momentum

Condition:

```text
downtrend_continuation:
  m0_40s_sell_ratio >= 0.64
  and m0_59s_low_time_frac >= 0.8

other:
  no extra agg filter

post_pump:
  no extra agg filter

momentum_dump:
  m0_59s_low_time_frac >= 0.8
  and m0_59s_close_pos <= 0.25
```

Validation:

- 226 trades, 2.86/day
- Win 55.8%
- Avg +1.11%
- PF 1.86
- Median MAE 1.92%

Use:

- More frequent than the higher-frequency combo, slightly worse PF.

## Family Findings

`downtrend_continuation`:

- Raw validation is weak after 0.30% cost: PF 1.13.
- It benefits the most from agg filtering.
- Best robust filter: `m0_40s_sell_ratio >= 0.64 and m0_59s_low_time_frac >= 0.8`.

`other`:

- Raw validation is already good: PF 2.38.
- Extra filtering often lowers frequency without necessary improvement.
- Keep it mostly unfiltered for frequency.

`post_pump`:

- Raw validation is acceptable but not outstanding: PF 1.48.
- Train-selected agg filters did not transfer well.
- Keep as ordinary signal if frequency is needed; do not mark as strong unless it also passes the strong agg quality filter.

`momentum_dump`:

- Raw validation is weak: PF 1.33.
- Some filters reduce damage but do not create a strong edge.
- Treat as optional / lower-priority.

## Practical Recommendation

Use a two-tier signal system:

1. Strong signal:
   - `m0_50s_sell_ratio >= 0.64 and m0_50s_close_pos <= 0.15`
   - Validation PF about 3.00 after conservative cost.

2. Normal tradable signal:
   - Family combo with downtrend filtered, other kept, post_pump kept.
   - Validation about 2.48/day and PF about 1.93 after conservative cost.

Do not use LGB as the current production agg filter. The model did not validate.

## Important Limitation

The current result proves aggTrade is useful as a microstructure quality filter for the executable 1m strategy.

It does not yet fully prove a production-safe pre-close entry system, because the base 1m candidate itself was still generated from closed 1m K-line rules. The next step is a replay where the partial current-minute candle is evaluated at 40s/50s with the same family rules and agg filters.

## Next Engineering Step

Implement a partial-candle replay:

- Build current 1m candle from aggTrade tick stream.
- Evaluate entry candidates at 40s/50s.
- Apply the same family combo filters.
- Exit using aggTrade tick prices for fast stop/lock.
- Compare against closed 1m baseline on the same covered symbol-days.
