# Binance Futures Waterfall Short Strategy Review Pack

## Latest Update: 2026-07-12

The newest corrected research pack is in:

- `LATEST_REVIEW_NOTES_20260712.md`
- `LATEST_MANIFEST_20260712.md`
- `results/latest_20260712/`
- `code/latest_20260712/`

This latest update incorporates the Claude audit fixes:

- Corrected USDT linear short PnL from `entry / exit - 1` to `1 - exit / entry`.
- Made 1m OHLC trailing-stop replay more conservative to avoid same-bar low/high ordering leakage.
- Split aggTrade research into `closed_1m` and strict `preclose_agg` modes.
- Added a trade-level agg pipeline design for evaluating agg filters against actual executable 1m trades.

The older sections below are kept as historical context from the previous review pack.

This folder is a standalone research pack for the Binance futures "waterfall dump" strategy. It is copied out of the main project so another AI or researcher can review the strategy without scanning the entire repository.

## Goal

The target strategy is an automated version of manual shorting on Binance USDT perpetual contracts:

1. Find contracts with waterfall potential.
2. Wait for a real high-volume downward break.
3. Enter short quickly enough to catch the main dump.
4. Avoid fake breakdowns and wick traps.
5. Exit dynamically so small dumps take small profit and large waterfalls can run.

The current production project is still not upgraded with this final aggTrade version. This pack is research-only.

## Current Best Finding

Direct aggTrade early entry is not good enough. It is faster, but it catches too many fake breaks.

The best current direction is:

```text
closed 1m structure signal
  + aggTrade sell-pressure filter on the signal minute
  + aggTrade fast stop / quick reclaim stop
  + 1m-based profit holding logic
```

In the latest 10 high-activity symbol test, the best practical candidate is:

```text
hybrid_micro_sell60:
  original 1m waterfall entry
  require signal-minute agg sell ratio >= 0.60
  use aggTrade only for faster stop / quick reclaim exit
  keep 1m logic for profit taking, so large waterfalls are not cut early
```

## Latest Same-Sample Result

Dataset:

- Symbols: top 10 largest local aggTrade symbols by downloaded zip size.
- Dates: 2026-06-13 to 2026-06-19.
- Families: post_pump, downtrend_continuation, momentum_dump, other.
- Source result: `results/waterfall_mode_compare_metrics_20260711_211535.json`

| Mode | Trades | Trades/day | Win rate | PF | Avg PnL | Avg MAE | Big 3% | Big 5% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| closed 1m baseline | 14 | 2.00 | 35.7% | 1.87 | +1.49% | 2.60% | 21.4% | 21.4% |
| agg direct early entry | 32 | 4.57 | 40.6% | 0.89 | -0.15% | 1.63% | 9.4% | 9.4% |
| hybrid full tick exit | 14 | 2.00 | 35.7% | 0.59 | -0.68% | 2.07% | 7.1% | 0.0% |
| hybrid stop only | 14 | 2.00 | 35.7% | 1.94 | +1.55% | 2.07% | 21.4% | 21.4% |
| hybrid preclose 55s | 23 | 3.29 | 21.7% | 7.59 | +13.48% | 4.32% | 13.0% | 8.7% |
| hybrid micro sell60 | 10 | 1.43 | 50.0% | 3.57 | +3.23% | 1.84% | 30.0% | 30.0% |
| hybrid micro strong | 4 | 0.57 | 75.0% | 15.18 | +9.35% | 1.76% | 75.0% | 75.0% |

Interpretation:

- `agg direct early entry` increases frequency but fails PF and average return.
- `hybrid full tick exit` exits profits too early and misses the waterfall body.
- `hybrid stop only` is a stable but small improvement over closed 1m.
- `hybrid_preclose` has high PF only because of a few very large winners; median and MAE are bad, so it is not yet robust.
- `hybrid_micro_sell60` is the best practical candidate so far.
- `hybrid_micro_strong` is a high-confidence tier, not a standalone main strategy because frequency is too low.

## 19-Day Microstructure Filter Search

Dataset:

- Local downloaded aggTrade symbols: 99.
- Dates: 2026-06-01 to 2026-06-19.
- Source result: `results/agg_entry_signal_filter_report_20260711_210033.json`

Baseline closed 1m entries:

- Trades: 55
- Win rate: 43.6%
- PF: 1.60
- Avg PnL: +0.82%
- Avg MAE: 2.42%

Best broader filters:

| Filter | Trades | Win rate | PF | Avg PnL | Avg MAE | Big 3% | Big 5% |
|---|---:|---:|---:|---:|---:|---:|---:|
| sell_ratio >= 0.60 and low_sec >= 0.75 | 20 | 65.0% | 3.89 | +2.89% | 2.38% | 40.0% | 20.0% |
| sell_ratio >= 0.60 and low_sec >= 0.65 | 24 | 62.5% | 3.49 | +2.48% | 2.37% | 33.3% | 16.7% |
| sell_ratio >= 0.60 | 35 | 57.1% | 2.44 | +1.57% | 2.20% | 22.9% | 11.4% |

Definitions:

- `sell_ratio`: in the signal minute, quote volume where buyer is maker, interpreted as taker sell pressure divided by total quote volume.
- `low_sec`: second index inside the signal minute when the low price occurred, normalized by 59. A high value means the low happened late in the minute rather than early wick-and-rebound.

## Current Strategy Recommendation

Use two tiers:

1. Main signal: `micro_sell60`
   - Trigger only when the original closed-1m waterfall signal is present and `agg_sell_ratio >= 0.60`.
   - Expected benefit: better PF, better average return, lower MAE, higher win rate than baseline in current tests.

2. Strong signal: `micro_strong`
   - Trigger when `agg_sell_ratio >= 0.60` and `agg_low_sec_frac >= 0.75`.
   - Expected benefit: very high quality, but lower frequency.

Do not use:

- Raw aggTrade early entry.
- Full tick-level trailing exit for winners.
- Preclose entry as the main strategy.
- Tight early adverse guards tested at 0.8%, 1.2%, 1.6%; they reduced MAE but cut too many winners.

## Important Caveats

The current evidence is promising but not enough for deployment:

- The latest full same-sample replay covers only 7 days.
- The 19-day filter search is more reliable for feature direction, but it is not yet a full strategy replay for all modes.
- Some aggTrade zip files are corrupt from an interrupted download; scripts skip bad zips for research, but the data should be repaired before final validation.
- Historical bookTicker data was not found in Binance Vision USDT-M daily files. bookTicker can still be collected live, but it cannot be honestly backtested unless historical order-book data is obtained.

## Suggested Next Experiments

1. Repair or redownload bad aggTrade zips.
2. Run full replay for `hybrid_micro_sell60` and `hybrid_micro_strong` on all available downloaded dates.
3. Compare train/validation by time:
   - Train/choose thresholds on 2026-06-01 to 2026-06-12.
   - Validate on 2026-06-13 to 2026-06-19.
4. Split results by family:
   - post_pump
   - downtrend_continuation
   - momentum_dump
   - other
5. Add live bookTicker shadow collection:
   - best bid/ask movement
   - spread widening
   - bid collapse
   - ask-side pressure
   - reclaim failure after breakdown
6. Only deploy after the new strategy beats closed 1m baseline on:
   - trades/day
   - win rate
   - PF
   - average PnL
   - MAE

## Files

Code snapshots:

- `code/waterfall.py`: production waterfall strategy engine snapshot.
- `code/compare_waterfall_replay_modes.py`: same-sample replay comparing closed 1m, agg direct, hybrid stop, preclose, and micro filters.
- `code/analyze_agg_entry_signal_filters.py`: extracts signal-minute aggTrade features and searches filters.
- `code/replay_aggtrade_waterfall.py`: direct aggTrade replay baseline.
- `code/search_agg_fast_filters.py`: earlier agg early-entry search.
- `code/download_binance_vision_aggtrades.py`: Binance Vision aggTrade downloader.

Results:

- `results/waterfall_mode_compare_metrics_20260711_211535.json`: latest 10-symbol full replay.
- `results/waterfall_mode_compare_detail_20260711_211535.csv`: per-symbol detail for latest full replay.
- `results/agg_entry_signal_filter_report_20260711_210033.json`: 19-day micro filter search report.
- `results/agg_entry_signal_filter_summary_20260711_210033.csv`: filter ranking table.
- `results/agg_entry_signal_features_20260711_210033.csv`: extracted signal-level micro features.

Config:

- `config/settings.json`: current config snapshot.
