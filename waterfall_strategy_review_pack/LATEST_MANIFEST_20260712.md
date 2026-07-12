# Latest Manifest 2026-07-12

Use this manifest together with `LATEST_REVIEW_NOTES_20260712.md`.

## Main Notes

- `LATEST_REVIEW_NOTES_20260712.md`

## Corrected Core 1m Results

- `results/latest_20260712/waterfall_report_formula_fixed_core5_20260712.md`
- `results/latest_20260712/waterfall_summary_formula_fixed_core5_20260712.json`
- `results/latest_20260712/waterfall_trades_formula_fixed_core5_20260712.csv`

The full event table is intentionally not copied into this review pack because it is about 80 MB:

- Source path: `backend/storage/ml/waterfall_patterns_formula_fixed_core5/waterfall_events_20260712_004858.csv`

## aggTrade Event Classifier Results

Strict pre-close agg model:

- `results/latest_20260712/agg_event_classifier_preclose_report_20260712.md`
- `results/latest_20260712/agg_event_classifier_preclose_summary_20260712.csv`

Closed-1m agg filter model:

- `results/latest_20260712/agg_event_classifier_closed1m_report_20260712.md`
- `results/latest_20260712/agg_event_classifier_closed1m_summary_20260712.csv`

## Core Trade agg Download Plan

- `results/latest_20260712/core_trade_agg_download_jobs.csv`

This CSV is the focused download list for extracting agg features on the exact 1555 executable 1m trades.

## Code Snapshots

Core strategy and corrected OHLC backtest:

- `code/latest_20260712/waterfall.py`
- `code/latest_20260712/discover_waterfall_patterns.py`
- `code/latest_20260712/compare_waterfall_replay_modes.py`
- `code/latest_20260712/optimize_waterfall_exit_profiles.py`

aggTrade event research:

- `code/latest_20260712/build_agg_event_features.py`
- `code/latest_20260712/train_agg_event_classifier.py`

aggTrade trade-level research, not finished yet:

- `code/latest_20260712/build_agg_features_for_waterfall_trades.py`
- `code/latest_20260712/train_agg_waterfall_trade_filter.py`
- `code/latest_20260712/download_agg_from_event_jobs.py`

## Suggested Review Order

1. Read `LATEST_REVIEW_NOTES_20260712.md`.
2. Audit `code/latest_20260712/discover_waterfall_patterns.py` for short PnL and OHLC execution ordering.
3. Read `results/latest_20260712/waterfall_report_formula_fixed_core5_20260712.md`.
4. Inspect `results/latest_20260712/waterfall_trades_formula_fixed_core5_20260712.csv` for outlier concentration.
5. Audit `code/latest_20260712/train_agg_event_classifier.py` and confirm `preclose_agg` excludes full current 1m K features.
6. Review whether `build_agg_features_for_waterfall_trades.py` + `train_agg_waterfall_trade_filter.py` is the right next experiment before production.
