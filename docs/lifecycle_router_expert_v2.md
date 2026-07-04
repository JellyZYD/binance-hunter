# Lifecycle Router Expert V2

This document describes the current production strategy upgrade.

## Purpose

The previous lifecycle expert called `fast_top`, `slow_warning`, `fast_short`, and `slow_short` on the same PumpWatch row, using only heuristic `behavior_state` gates. That made top/short alerts appear during rising or sideways phases.

V2 adds a learned family-probability route before any top/short expert is allowed to run.
The current upgrade also separates broad discovery from formal PumpWatch. Broad REST discovery is kept as a shadow watch pool so the system can keep updating highs, but top/short experts and the main active dashboard only arm after lifecycle max gain reaches 25%. A stricter high-pump pre-router expert is still reserved for coins that have already reached a 40% pump.

## Production Flow

1. REST discovery creates `LongEvent` and `PumpEvent` watch entries.
2. WebSocket closed candles update state:
   - long entry uses closed `5m` candles;
   - top/short lifecycle experts use closed `15m` candles.
3. Every active PumpWatch builds one lifecycle row from past data only.
4. PumpWatch entries below 25% lifecycle max gain stay in `shadow_watch`: they update highs and history, but do not call top/short experts and are hidden from the main active-contract board by default.
5. If the PumpWatch has reached the high-pump threshold, `high_top` / `high_short` rebuild context from the first 40% crossing and may emit one lifecycle-level signal before the family router confirms.
6. `family_router` scores:
   - `fast_dump`
   - `slow_distribution`
   - `second_distribution`
   - `continuation`
   - `normal_reversal`
7. `route_from_probabilities` converts those probabilities into an abstaining production route.
8. The same non-unknown route must hold for 2 consecutive 15m bars.
9. Only then can the corresponding expert run.

## Route Policy

| Route | Production Action |
| --- | --- |
| `unknown` | Watch only; no top/short expert. |
| `continuation` | Watch only; short/top blocked. |
| `second_distribution` | Watch only for now; sample size is not enough for a production short expert. |
| `fast_dump` | Allows `fast_top`; `fast_short` is strict. |
| `slow_distribution` | Allows internal `distribution_warning`; only `slow_short` in `breakdown` can emit `short_signal`. |

## Current Thresholds

| Item | Value |
| --- | ---: |
| `strategy_version` | `lifecycle_router_expert` |
| route confirm bars | `2` |
| route margin | `0.12` |
| `fast_dump` route threshold | `0.914496` |
| `slow_distribution` route threshold | `0.701967` |
| `second_distribution` route threshold | `0.72` |
| Formal PumpWatch top/short signal min gain | `25%` |
| high-pump reset threshold | `40%` |
| `high_top` threshold | `0.216917` |
| `high_short` threshold | `0.595417` |
| `fast_top` threshold | `0.523018` |
| `fast_short` threshold | `0.700000` |
| `slow_warning` threshold | `0.897677` |
| `slow_short` threshold | `0.589188` |

Dynamic thresholds are conservative. Trend-hold and acceleration states raise route thresholds; production does not lower route thresholds in breakdown because replay showed that lowered thresholds increased short adverse movement.

## Pump Admission Experiment

The latest admission experiment compared current broad discovery against stricter lifecycle max-gain gates. The 40% gate was too narrow and missed too many test lifecycles; window-only rules around 25% were still too broad because they can be triggered by normal rolling-window noise.

Selected production policy:

- Broad discovery remains unchanged enough to keep the watch anchor and high updated.
- Formal PumpWatch starts at lifecycle max gain `>=25%`.
- PumpWatch derived from a fired long signal keeps the existing `15%` maturity rule, because it is used for flat-long / turn-short monitoring after an actual long alert.
- Main `/api/pumps` and the active-contract board show formal PumpWatch only by default.
- `include_shadow=1` on `/api/pumps` can still be used for diagnostics.
- The 40% high-pump expert remains a separate special case.

Experiment summary (`backend/storage/ml/pump_admission_optimization_exact_life25`):

| Rule | All Admission | Test Admission | Retained Test Short | Median Short Drop24 | Median Short Adv24 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Current proxy | 87.0% | 88.9% | 1 | 17.0% | 4.4% |
| Lifecycle high >=25% | 71.2% | 72.2% | 1 | 17.0% | 4.4% |
| Lifecycle high >=40% | 44.6% | 41.7% | 1 | 17.0% | 4.4% |

This reduces formal monitoring clutter while retaining the sparse clean short observed in the holdout replay.

## Replay Result

Command:

```bash
PYTHONPATH=backend python backend/ml_experiments/backtest_lifecycle_router_replay.py
```

Dataset:

- `backend/storage/ml/dense_lifecycle/dense_15m.parquet`
- Holdout test: 13,583 rows, 36 lifecycles
- Replay uses current production model files and closed 15m rows only.

First signal per lifecycle/level:

| Segment | Signals | Median Up24 | Median Drop6 | Median Drop24 | Median Drop72 | Median Short Adv24 | Clean Short24 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `distribution_warning / slow_warning` | 4 | 12.9% | 4.5% | 4.5% | 4.5% | 6.7% | 25.0% |
| `early_alert / fast_top` | 3 | 7.6% | 22.2% | 26.2% | 42.0% | 7.6% | 33.3% |
| `short_signal / slow_short` | 2 | 2.6% | 4.9% | 12.2% | 12.8% | 2.6% | 50.0% |

Short-signal total:

- signals: 2
- median 24h adverse: 2.6%
- median 24h drop: 12.2%
- median 72h drop: 12.8%

## Dynamic Threshold Experiment

Three route-threshold modes were replayed:

| Mode | First Short Signals | Median Up24 | Median Drop24 | Clean Short24 | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Static strict | 3 | 4.4% | 9.4% | 33.3% | Baseline. |
| Lower fast and slow on breakdown | 5 | 16.9% | 9.4% | 40.0% | Rejected: too much adverse movement. |
| Lower slow only | 4 | 10.6% | 8.4% | 25.0% | Rejected: worse adverse and lower quality. |
| Conservative dynamic | 2 | 2.6% | 12.2% | 50.0% | Selected. |

The selected production version is intentionally sparse. The goal is to stop rising/sideways false shorts first; more coverage should only be added after a new expert proves low adverse movement in replay.

## High-Pump Expert Experiment

The high-pump experiment rebuilds every lifecycle from the first time it reaches a 40% gain. This directly targets the issue where the family router confirms too late and misses the real top.

Training command:

```bash
PYTHONPATH=backend python backend/ml_experiments/train_high_pump40_experts.py
```

Dataset:

- Source dense rows: `backend/storage/ml/dense_lifecycle/dense_15m.parquet`
- High-pump reset rows: `42,335`
- High-pump lifecycles: `160`
- Holdout uses closed 15m rows only.

Selected production settings:

| Expert | Role | Threshold | Production Policy |
| --- | --- | ---: | --- |
| `high_top` | 40% pump top / long-risk alert | `0.216917` | Emits at most once per PumpWatch lifecycle. It is a top/flat-long warning, not an automatic short. |
| `high_short` | 40% pump breakdown short candidate | `0.595417` | Strict model is packaged but currently sparse in production replay; normal `slow_short` remains the main short signal. |

Production replay with high-pump q85:

| Segment | Signals | Median Up24 | Median Drop6 | Median Drop24 | Median Short Adv24 | Clean Short24 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `early_alert / high_top` | 4 | 29.8% | 12.5% | 19.5% | 7.3% | 50.0% |
| `early_alert / fast_top` | 3 | 7.6% | 22.2% | 26.2% | 7.6% | 33.3% |
| `short_signal / slow_short` | 2 | 2.6% | 4.9% | 12.2% | 2.6% | 50.0% |

Why q85:

- q80 covers one more lifecycle but is too early and noisier.
- q90 is cleaner but only emits one high-pump signal in the current production replay.
- q85 is the current compromise; it should be treated as a risk/flat-long signal, while actual short remains tied to breakdown-style signals.

## Push And UI

WeCom pushes only:

- `long_signal`
- `early_alert`
- `short_signal`

`distribution_warning` is not pushed by default. It is kept as lifecycle state/evidence for the dashboard.

The dashboard active contract card shows:

- lifecycle mode;
- behavior state;
- confirmed route as a compact tag;
- candidate/streak/confidence/margin/probabilities only in hover/debug detail, not as visible card text;
- high-pump enabled/min-gain and PumpWatch signal min-gain in the strategy/model bars.
