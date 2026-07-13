# ML sequence experiments

This directory is intentionally separate from the live rule engine. It is for
local training and offline comparison only. The server can later load an
exported `model.pt` bundle for inference, but no live path imports these files
today.

## Exact production Board replay

`backtest_board_waterfall.py` is the exception to the research-only rule: it
imports the production `BoardWaterfallEngine` directly so replay and live paper
logic cannot drift. It merges symbols in timestamp order and preserves global
account equity, free margin, position limits and cooldowns.

```powershell
python backend/ml_experiments/backtest_board_waterfall.py `
  --klines-dir "E:\A\bb\data\klines" `
  --start 2026-01-01 --end 2026-06-30 `
  --split-date 2026-04-01
```

This runner is the authoritative path for validating the Board strategy's
headline metrics. Other scripts in this directory remain exploratory unless a
production document explicitly says otherwise.

## Models worth testing

Use these after the rule engine has already found pump/watch candidates:

- `cnn_small` / `cnn_wide`: fastest baselines for local candle shape.
- `tcn_small` / `tcn_deep` / `tcn_wide`: causal convolution models for 15m
  sequence patterns.
- `gru_small` / `gru_medium` / `gru_stack`: recurrent models; good candidates
  when the signal depends on the order of topping bars.
- `tiny_transformer` / `transformer_wide` / `transformer_deep`: sequence
  attention models. Train locally only; these are still small enough for CPU
  inference on 2C2G when used on a limited watchlist.

Do not train these on the 2C2G server. Train locally, copy the best `.pt` bundle
to the server, then run only inference.

## Build a dataset

```powershell
cd E:\A\pixel-canvas\backend

python ml_experiments\build_sequence_dataset.py `
  --source "E:\A\币安数据库" `
  --out storage\ml\seq_365_state.npz `
  --days 365 `
  --seq-len 64 `
  --include-state
```

For a quick smoke test:

```powershell
python ml_experiments\build_sequence_dataset.py `
  --source E:\A\bb\data `
  --out storage\ml\seq_smoke.npz `
  --days 30 `
  --max-symbols 5 `
  --seq-len 32 `
  --stride 4 `
  --max-samples 3000 `
  --include-state
```

The builder:

- excludes major coins, stock contracts, and metals using the same config list
  plus `DEFAULT_NON_ALT_SYMBOLS`;
- aggregates 1m klines into closed 15m bars;
- keeps only watch-state bars where recent 24h runup is at least 20%;
- creates two labels:
  - `top`: future 24h drop >= 8% and adverse rise <= 8%;
  - `dump`: future 12h drop >= 8% and adverse rise <= 4%;
- optionally merges funding, OI, top position ratio, global account ratio, and
  taker ratio with `--include-state`.

## Train models in parallel

```powershell
python ml_experiments\train_sequence_models.py `
  --dataset storage\ml\seq_365_state.npz `
  --task dump `
  --out-dir storage\ml\runs_dump `
  --jobs 4 `
  --epochs 20 `
  --batch-size 512
```

Train top separately:

```powershell
python ml_experiments\train_sequence_models.py `
  --dataset storage\ml\seq_365_state.npz `
  --task top `
  --out-dir storage\ml\runs_top `
  --jobs 4 `
  --epochs 20 `
  --batch-size 512
```

Ranking files:

- `storage/ml/runs_dump/ranking_dump.json`
- `storage/ml/runs_top/ranking_top.json`

Primary comparison metrics:

- `val_rank_score` / `test_rank_score`
- `val_auc` / `test_auc`
- `val_ap` / `test_ap`
- `p_at_20`, `p_at_50`, `p_at_100`, `p_at_500`
- `p_top_1pct`, `p_top_5pct`

For this strategy, top-bucket precision matters more than global AUC. The
ranking score intentionally weights AP and top-bucket precision, not just AUC.

## Inference

Each run writes:

```text
storage/ml/runs_<task>/<task>_<model>/model.pt
```

The bundle contains:

- model architecture name and params;
- feature names;
- sequence length;
- train-set mean/std scaler;
- PyTorch state dict.

Smoke inference:

```powershell
python ml_experiments\predict_sequence.py `
  --model storage\ml\runs_dump\dump_tcn_small\model.pt `
  --input storage\ml\seq_smoke.npz `
  --out storage\ml\predictions.npy
```

## Lifecycle experiment

`train_lifecycle_models.py` is the end-to-end long-to-short research pipeline:

- `long_pump_event`: early long-entry score, predicting whether the entry becomes
  a historical pump event within 48h.
- `long_start_quality`: quality/risk tier for entries that rally without first
  chopping hard against the position.
- `family`: dynamic pump-family probabilities after the long entry.
- `flat_long`: long-exit / take-profit warning.
- `short_start`: short-entry start score.

Run:

```powershell
python -m ml_experiments.train_lifecycle_models `
  --source "E:\2C2G\币安数据库" `
  --days 365
```

Outputs:

- `storage/ml/lifecycle_models.md`
- `storage/ml/lifecycle_models.json`
- `storage/ml/lifecycle/long_entries.parquet`
- `storage/ml/lifecycle/state_rows.parquet`
- `storage/ml/lifecycle_models/*.txt`

## Lifecycle sequence experiment

`train_lifecycle_sequence_models.py` tests whether long lookback sequence
models improve the lifecycle start signal and dynamic family classification:

- `long_pump_event`: GRU/TCN/Transformer on the 48h sequence before entry.
- `family`: GRU/TCN/Transformer on the 48h sequence ending at each post-entry
  decision bar, with entry-relative context channels.

Run the pure price/volume/taker baseline:

```powershell
python -m ml_experiments.train_lifecycle_sequence_models `
  --source "E:\2C2G\币安数据库" `
  --models tcn_small,gru_medium,tiny_transformer `
  --epochs 12 `
  --patience 4 `
  --rebuild
```

Run the GRU state-feature variant:

```powershell
python -m ml_experiments.train_lifecycle_sequence_models `
  --source "E:\2C2G\币安数据库" `
  --models gru_medium `
  --include-state `
  --dataset-dir storage/ml/lifecycle_seq_state `
  --out-dir storage/ml/lifecycle_seq_runs_state `
  --epochs 12 `
  --patience 4 `
  --rebuild
```

Outputs:

- `storage/ml/lifecycle_seq_runs/lifecycle_sequence_results.md`
- `storage/ml/lifecycle_seq_runs/lifecycle_sequence_results.json`
- `storage/ml/lifecycle_seq_runs/**/model.pt`

## Dynamic family hierarchical experiment

`train_dynamic_family_hierarchical.py` upgrades the mixed 5-way dynamic family
classifier into operational binary sequence experts:

- `fast_dump`: urgent dump family.
- `slow_or_second_distribution`: slow distribution or second distribution.
- `continuation`: continuation / avoid-short family.

It also reports metrics by contiguous lifecycle stage buckets:

- `early_0_8h`
- `mid_8_24h`
- `distribution_24_48h`
- `late_48_72h`

Run global binary experts plus stage-specific training:

```powershell
python -m ml_experiments.train_dynamic_family_hierarchical `
  --dataset storage/ml/lifecycle_seq/lifecycle_seq_family.npz `
  --models gru_medium `
  --epochs 14 `
  --patience 5 `
  --stage-specific `
  --out-dir storage/ml/dynamic_family_stage_experts
```

Latest read: global sequence models are more stable than hard stage-specific
training. Early-stage family classification is weak, especially for
`slow_or_second_distribution`; late-stage probabilities are much more useful for
confirming an established distribution/dump path. Use stage-aware thresholds or
a gate on top of the global model before considering production integration.

Model comparison read:

- `fast_dump`: `gru_stack` gives the best overall high-confidence bucket
  (`q95` around 92%), while `tiny_transformer` has slightly stronger AUC and
  similar late-stage confirmation. The Transformer is slower and does not solve
  early-stage uncertainty.
- `slow_or_second_distribution`: `gru_stack` is the best overall candidate
  (`q95` around 65%). `tiny_transformer` is weaker overall but can be strong in
  the late stage, so keep it as an experiment-only auxiliary check.
- `continuation`: do not use as a hard continuation/hold-long signal with the
  current label. The holdout positive rate is too low and top buckets are not
  actionable.

Outputs:

- `storage/ml/dynamic_family_stage_experts/dynamic_family_hierarchical.md`
- `storage/ml/dynamic_family_stage_experts/dynamic_family_hierarchical.json`
- `storage/ml/dynamic_family_stage_experts/**/model.pt`
- `storage/ml/dynamic_family_model_compare/dynamic_family_hierarchical.md`
- `storage/ml/dynamic_family_model_compare/dynamic_family_hierarchical.json`

## Dynamic behavior-state experiment

`train_dynamic_behavior_state.py` replaces fixed lifecycle hour buckets with
live-observable behavior states:

- `acceleration`: rally is still pressing near highs.
- `climax_risk`: extended rally with topping/failure symptoms.
- `distribution`: high-level churn after a pump.
- `breakdown`: high-to-low structure has broken.
- `pullback_risk`: fast pullback not yet full breakdown.
- `trend_hold`: healthy rally / hold-long region.
- `neutral_watch`: no strong behavior state.

Run:

```powershell
python -m ml_experiments.train_dynamic_behavior_state `
  --models gru_stack `
  --epochs 12 `
  --patience 4 `
  --out-dir storage/ml/dynamic_behavior_state
```

Latest read:

- Behavior states are more useful than fixed `0-8h/8-24h/...` buckets for
  routing decisions.
- `fast_dump + breakdown` is the strongest short-side path: holdout `q90/q95`
  precision reaches 100% inside `breakdown`.
- `slow_or_second + distribution` is the strongest slow-distribution path:
  holdout `q95` precision is around 77% inside `distribution`.
- Direct `short_start` and `flat_long` labels are not strong enough as primary
  gates with the current sequence GRU. Use them only as auxiliary evidence.
- `continue_long` is useful only as auxiliary hold-long evidence in
  `trend_hold/acceleration`; it should not override breakdown/distribution risk.

Outputs:

- `storage/ml/dynamic_behavior_state/dynamic_behavior_state.md`
- `storage/ml/dynamic_behavior_state/dynamic_behavior_state.json`
- `storage/ml/dynamic_behavior_state/**/model.pt`

## Mode-aware lifecycle experiment

`train_mode_aware_lifecycle.py` tests the stricter framework:

1. Train an entry model that only wants lifecycles belonging to the target
   manipulation modes: `fast_dump`, `slow_distribution`, `second_distribution`.
2. Train a dynamic router that estimates which mode the evolving lifecycle
   currently resembles.
3. Train separate per-mode `top_exit` and `short_clean` experts. These experts
   are judged by adverse move, near-term drop, and event coverage, not only AUC.

Run:

```powershell
python -m ml_experiments.train_mode_aware_lifecycle `
  --epochs 8 `
  --patience 3 `
  --out-dir storage/ml/mode_aware_lifecycle
```

Latest read:

- The stricter entry model is not good enough yet. `target_mode_entry` improves
  over base rate, but the top bucket still contains too many `none` and
  `normal_reversal` rows. The current startup label/model does not yet satisfy
  "long signal should mostly become one of the target manipulation lifecycles".
- The router can separate `fast_dump` and `slow_distribution` moderately, but
  `second_distribution` is too sparse in the holdout split to be trusted as a
  standalone routed mode.
- The only currently promising per-mode expert is `fast_dump/short_clean`:
  high-confidence holdout rows show low median adverse and useful near-term
  drop. `slow_distribution` top/short experts are weak under the current labels.
- This experiment exposes a data-design issue: `state_rows.parquet` samples only
  fixed lifecycle checkpoints (`0/2/4/6/8/12/18/24/36/48/60/72h`). It is not dense
  15m replay data, so it cannot prove "each lifecycle triggers once" or catch the
  true first breakdown moment. The next redesign should build dense 15m lifecycle
  rows from entry to 72h and evaluate first-signal coverage per lifecycle.

Outputs:

- `storage/ml/mode_aware_lifecycle/mode_aware_lifecycle.md`
- `storage/ml/mode_aware_lifecycle/mode_aware_lifecycle.json`
- `storage/ml/mode_aware_lifecycle/**/model.pt`
- `storage/ml/mode_aware_lifecycle/experts/**/*.txt`

## Dense 15m lifecycle expert experiment

`train_dense_lifecycle_experts.py` is the next step after the sparse
mode-aware run. It rebuilds each target lifecycle as every closed 15m bar from
long entry to +72h, then trains separate LightGBM top/short experts per
manipulation mode:

- `fast_dump`
- `slow_distribution`
- `second_distribution`

This is still experiment-only. It does not change production signals.

Run a smoke test:

```powershell
python -m ml_experiments.train_dense_lifecycle_experts `
  --rebuild `
  --max-entries 120 `
  --dataset-out storage/ml/dense_lifecycle/smoke_dense_15m.parquet `
  --out-dir storage/ml/dense_lifecycle_smoke `
  --min-train-rows 50 `
  --min-train-positives 5
```

Run the full dense replay:

```powershell
python -m ml_experiments.train_dense_lifecycle_experts `
  --rebuild `
  --dataset-out storage/ml/dense_lifecycle/dense_15m.parquet `
  --out-dir storage/ml/dense_lifecycle_experts_gated
```

If the dense parquet already exists, omit `--rebuild` to retrain/re-evaluate
experts quickly:

```powershell
python -m ml_experiments.train_dense_lifecycle_experts `
  --dataset-out storage/ml/dense_lifecycle/dense_15m.parquet `
  --out-dir storage/ml/dense_lifecycle_experts_gated
```

Run the dynamic-label version on the same dense replay:

```powershell
python -m ml_experiments.train_dense_lifecycle_experts `
  --dataset-out storage/ml/dense_lifecycle/dense_15m.parquet `
  --label-mode dynamic `
  --out-dir storage/ml/dense_lifecycle_experts_dynamic
```

Run the current hybrid-label candidate:

```powershell
python -m ml_experiments.train_dense_lifecycle_experts `
  --dataset-out storage/ml/dense_lifecycle/dense_15m.parquet `
  --label-mode hybrid `
  --out-dir storage/ml/dense_lifecycle_experts_hybrid
```

What this experiment fixes:

- splits by lifecycle `entry_time`, not individual decision rows, so the same
  pump lifecycle cannot leak across train/validation/test;
- uses only already-closed 15m candles for decision features;
- uses future path only for labels and evaluation metrics;
- evaluates first signal per lifecycle using validation-derived `q80/q90/q95`
  thresholds, instead of only row-level AUC;
- reports behavior-state gated first-signal replay:
  - `all`
  - `breakdown`
  - `breakdown_pullback`
  - `distribution_climax_pullback`

Label modes:

- `fixed`: the original per-mode thresholds, such as fixed 6h/24h/72h drop and
  fixed adverse caps.
- `dynamic`: row-specific labels. The target drop, allowed adverse move, minimum
  pump amplitude, and required high-to-low drawdown scale with the current
  closed-candle lifecycle context (`ctx_high_since_entry`,
  `ctx_drawdown_from_entry_high`) and current noise (`atr_14`, `retstd_20`).
  Future path is still used only as the training target and replay evaluation,
  not as a model feature.
- `hybrid`: dynamic labels for `fast_dump`, fixed labels for
  `slow_distribution` and `second_distribution`.

Latest full run:

- Dataset: `storage/ml/dense_lifecycle/dense_15m.parquet`
- Rows: 113,577
- Lifecycles: 393
- Feature count: 103
- Report:
  `storage/ml/dense_lifecycle_experts_gated/dense_lifecycle_experts.md`

Latest read:

- `fast_dump/top_exit` is only useful when gated to
  `distribution_climax_pullback`. In the latest holdout first-signal replay,
  `q90` covered 33.3% of fast-dump lifecycles with 80.0% precision, median
  24h adverse up only 0.8%, and median 24h/72h drop of 27.0%/70.5%. Treat this
  as the current best high-confidence flat-long / top-warning candidate.
- `fast_dump/short_clean` should also prefer `distribution_climax_pullback`,
  not raw `breakdown`. The latest `q80` gate covered 57.1% with 50.0%
  precision, median 6h/24h short adverse only 0.8%/0.8%, and median 24h/72h
  drop of 32.5%/74.0%. This is the cleanest current short-entry candidate.
- `slow_distribution/top_exit` is not good enough as an exact top signal. Even
  the best behavior gate still has high median adverse up around 12-18%. Use it
  only as distribution caution until the label/model is redesigned.
- `slow_distribution/short_clean` is usable but lower confidence than fast
  dump. The best current gate is `distribution_climax_pullback/q90-q95`, with
  median 24h adverse around 5.9-7.7% and median 24h drop around 10.5-11.8%.
- `second_distribution` is still too sparse in the current split and is skipped
  by the dense expert trainer. Merge it with slow distribution or collect more
  lifecycles before training it as a standalone routed mode.

Latest dynamic-label read:

- Dynamic labels are not a universal replacement for fixed labels. They improve
  the fast-dump experts but degrade slow-distribution experts in the latest
  holdout.
- `fast_dump/top_exit` with `distribution_climax_pullback/q80` covered 33.3% of
  fast-dump lifecycles, reached 80.0% first-signal precision, and fired much
  earlier than the fixed-label top model. Median 24h adverse up was 3.2%, while
  median 6h/24h/72h drop was 38.0%/54.2%/54.2%. This is a strong candidate for
  an earlier fast-dump flat-long/top-warning model.
- `fast_dump/short_clean` with `distribution_climax_pullback/q90` covered 38.5%,
  reached 60.0% first-signal precision, and had median 6h/24h short adverse of
  2.0%/2.0%. Median 6h/24h drop was 14.6%/22.2%. This is cleaner than the fixed
  q90 fast short on the latest replay, though coverage is lower.
- `slow_distribution/top_exit` got worse under the current dynamic label. It
  still has too much adverse and poor precision, so do not use it as a top
  signal.
- `slow_distribution/short_clean` also got weaker than the fixed-label version.
  Keep fixed labels for slow short until the slow-distribution label is
  redesigned around a different path definition.
- Current best direction: hybrid labeling, not one global label mode. Use
  dynamic labels for fast-dump top/short; keep fixed labels for slow short; keep
  slow top disabled or caution-only.

Latest hybrid-label output:

- Report:
  `storage/ml/dense_lifecycle_experts_hybrid/dense_lifecycle_experts.md`
- JSON:
  `storage/ml/dense_lifecycle_experts_hybrid/dense_lifecycle_experts.json`
- Recommended research baseline from this round:
  - `fast_dump/top_exit`: dynamic labels +
    `distribution_climax_pullback/q80`
  - `fast_dump/short_clean`: dynamic labels +
    `distribution_climax_pullback/q90` for cleaner signals, or `q80` for more
    coverage
  - `slow_distribution/short_clean`: fixed labels +
    `distribution_climax_pullback/q90-q95`
  - `slow_distribution/top_exit`: disabled or caution-only
  - `second_distribution`: still insufficient standalone data

## Slow-distribution-specific expert experiment

`train_slow_distribution_experts.py` redesigns slow distribution as a high-level
range problem instead of a fast-dump top problem:

- `distribution_warning`: high-zone / do-not-chase-long warning.
- `breakdown_short`: high range has started to fail with acceptable short
  adverse.

Run:

```powershell
python -m ml_experiments.train_slow_distribution_experts `
  --dataset storage/ml/dense_lifecycle/dense_15m.parquet `
  --out-dir storage/ml/slow_distribution_experts
```

Latest output:

- Report:
  `storage/ml/slow_distribution_experts/slow_distribution_experts.md`
- JSON:
  `storage/ml/slow_distribution_experts/slow_distribution_experts.json`

Latest read:

- `distribution_warning` is better as a defensive high-zone warning than an
  actionable top. The cleanest slow-only setting is `low_adverse` with
  `distribution_pullback/q95`: coverage 35.3%, precision 66.7%, median 24h
  adverse/upside 5.2%, and median 24h/72h drop 10.6%/11.0%. This is not yet as
  strong as fast-dump top, but it is usable as "do not chase / prepare exit"
  evidence.
- `breakdown_short` is the useful slow-distribution model. The strict
  slow+second `late_break` setting with `distribution_pullback/q90` reached
  coverage 45.5%, precision 100.0%, median 24h short adverse 2.6%, and median
  24h/72h drop 9.6%/9.7%. This is a practical slow-breakdown short candidate,
  but it fires late by design.
- A broader slow-only alternative is `mature_range` with
  `distribution_pullback/q80`: coverage 76.9%, precision 70.0%, median 24h
  adverse 5.2%, and median 24h drop 8.3%. Use this only as a lower-confidence
  setup because adverse is higher.
- Do not treat slow high-zone warning as a short entry. It is a separate
  defensive state; short still needs `breakdown_short` confirmation.

## Dynamic router threshold optimization

`optimize_dynamic_router_thresholds.py` tests fixed behavior states against
dynamic threshold configurations. It evaluates the router itself before a full
signal stack, using first lifecycle entry into risk gates.

Run:

```powershell
python -m ml_experiments.optimize_dynamic_router_thresholds `
  --dataset storage/ml/dense_lifecycle/dense_15m.parquet `
  --out-dir storage/ml/dynamic_router_thresholds
```

Latest output:

- Report:
  `storage/ml/dynamic_router_thresholds/dynamic_router_thresholds.md`
- JSON:
  `storage/ml/dynamic_router_thresholds/dynamic_router_thresholds.json`

Latest read:

- Dynamic router thresholds beat the fixed router for short-side routing. The
  best config is `dyn_big_pump_tolerant`.
- Fixed router first-risk-gate baseline:
  - fast short coverage 75.7%, precision 23.2%, median 24h short adverse 10.1%,
    median 24h drop 13.7%.
  - slow short coverage 75.7%, precision 24.5%, median 24h adverse 5.8%,
    median 24h drop 5.6%.
- `dyn_big_pump_tolerant`:
  - fast short coverage 95.9%, precision 31.0%, median 24h adverse 5.9%, median
    24h drop 20.2%.
  - slow short coverage 95.8%, precision 30.9%, median 24h adverse 5.5%, median
    24h drop 6.7%.
- Router-only fast top precision remains poor; top quality still needs the
  expert model score, not state gating alone.
- Current router candidate for future integration:
  `dyn_big_pump_tolerant`, then expert scoring on top.

Current action boundary:

- Do not deploy these dense experts yet.
- Use this report to tune labels and gates first.
- Production is still the old signal stack until a later integration step
  explicitly loads these expert models.
