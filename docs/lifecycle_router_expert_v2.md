# Lifecycle Router Expert V2

This document describes the current production strategy upgrade.

## Purpose

The previous lifecycle expert called `fast_top`, `slow_warning`, `fast_short`, and `slow_short` on the same PumpWatch row, using only heuristic `behavior_state` gates. That made top/short alerts appear during rising or sideways phases.

V2 adds a learned family-probability route before any top/short expert is allowed to run.

## Production Flow

1. REST discovery creates `LongEvent` and `PumpEvent` watch entries.
2. WebSocket closed candles update state:
   - long entry uses closed `5m` candles;
   - top/short lifecycle experts use closed `15m` candles.
3. Every active PumpWatch builds one lifecycle row from past data only.
4. `family_router` scores:
   - `fast_dump`
   - `slow_distribution`
   - `second_distribution`
   - `continuation`
   - `normal_reversal`
5. `route_from_probabilities` converts those probabilities into an abstaining production route.
6. The same non-unknown route must hold for 2 consecutive 15m bars.
7. Only then can the corresponding expert run.

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
| `fast_top` threshold | `0.523018` |
| `fast_short` threshold | `0.700000` |
| `slow_warning` threshold | `0.897677` |
| `slow_short` threshold | `0.589188` |

Dynamic thresholds are conservative. Trend-hold and acceleration states raise route thresholds; production does not lower route thresholds in breakdown because replay showed that lowered thresholds increased short adverse movement.

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

## Push And UI

WeCom pushes only:

- `long_signal`
- `early_alert`
- `short_signal`

`distribution_warning` is not pushed by default. It is kept as lifecycle state/evidence for the dashboard.

The dashboard active contract card shows:

- lifecycle mode;
- behavior state;
- confirmed route;
- route candidate/streak;
- route confidence/margin;
- fast/slow route probabilities.
