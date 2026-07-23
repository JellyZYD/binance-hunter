# Board-Waterfall Strategy (Claude·冠军标签)

The sole production waterfall signal engine, consuming closed 1m WebSocket
candles and driving three independent 100U paper accounts. The former Codex
`core5_agg` engine is disabled and retained only as research history.
Engine: `backend/pump_dump_hunter/board_waterfall.py`, strategy id
`claude_board_wf_1m`.

> For the full research trail and the transferable lessons behind every design
> choice here (label, entry timing, exit, cooldown, and the backtest traps we
> hit), see **[waterfall_lessons.md](waterfall_lessons.md)**.

## Why this is the production engine

Three days of independent research (audit → label refinement → walk-forward)
converged on the board-coin deep-waterfall label. Subsequent audit found the
core5 path overfit, so production no longer combines it with this engine.

## Label (walk-forward validated 2023-2025 select / 2026H1 verdict)

A short opens on the confirming 1m close when all hold:

- **board coin**: 24h return ≥ +40% (`min_ret_24h`)
- **detection**: close ≤ 60m rolling high × (1 − 7%) (`break_window_min`,
  `break_drop`)
- **liquidity**: 60m quote volume ≥ 300k USDT (`min_qv60_usdt`)

Derived from a 27-definition sweep where drop depth (3/5/7%) and board
threshold (10/20/40%) were both monotonic — the +40%/−7% corner is the
highest-EV, highest-purity point (49% base true-waterfall rate vs 32% for the
old label). This is the user's own manual recipe, quantified.

Tick-early entry was **rejected**: 62% of intra-minute −7% breaks are wicks that
close back above, and the wick tax eats the entry-price improvement. Entry
waits for the 1m close.

All time windows are timestamp-based, not row-count-based. The 24h return must
reference the candle exactly 1,440 minutes earlier, and both the 1,441-candle
24h window and the 60m break window must be contiguous. If any required closed
1m candle is missing, entry fails closed while prewarm repairs the gap from
REST. This keeps paper, replay and live execution deterministic even when one
WebSocket minute was missed.

## Exit (E1 base + flow-gated hold-through)

Base structure (E1, winner of an 8-variant battle):

- **structure stop** at `B × 1.01` where B = highest price after the 60m low
  (min `entry × 1.015`). This is the user's "bounce back to origin = wick" rule.
  Always active — the catastrophic-loss backstop, never gated.
- **trailing profit**: activate at MFE ≥ 3.5%, rebound 3.0%, prev-bar-confirmed
  (no same-bar lookahead).
- **time stop**: 240 minutes.

When a stop or trailing trigger has already been crossed before the next
closed-bar decision can act, paper execution now fills at the worse of the
trigger and that candle's open, then applies exit slippage. It never books a
historical trigger price that was no longer executable.

**Flow-gated hold-through** (`exit_flow_gate_enabled`, default on): the trailing
take-profit is SKIPPED when taker-sell over the prior `exit_flow_window` (=10)
closed bars is still ≥ `exit_flow_sell_threshold` (=0.48) — sellers in control ⇒
the bounce is fake ⇒ hold for the next leg down. When buyers return (flow drops
below the threshold) the trail fires as normal. The stop is never gated, so a
held-through trade can never lose more than E1 would.

The original flow-gate research appeared to reopen the "big-meat ceiling", but
the 2026-07-24 executable-price audit found that result was not tradable. The
replay delayed the take-profit while sellers remained dominant, then filled at
the stale low trigger after price had already rebounded through it. On the same
3,629-trade path, correcting only that fill assumption changed PF from 1.565 to
0.958 and average return from +1.372% to -0.112%. The flow gate remains active
for controlled micro-live observation because the user requested no strategy
parameter change in this release, but the old `+1.44%/trade` claim is retired
and must not be used to size capital.

A 15-cell exchange-wide trailing guard grid (5%-8% activation, 4%-5% callback)
did not improve train and holdout together. Exact aggTrade replay confirmed that
it reduced some giveback but also cut large winners. No third protection order
was enabled. See
[`champion/04-止盈可执行性审计-20260724.md`](champion/04-止盈可执行性审计-20260724.md).

**Cooldown 6h → 20m** (`same_symbol_cooldown_hours` = 0.3333,
`max_trades_per_symbol_day` = 8): a genuine SECOND waterfall (a fresh +40%/−7%
break after we exit) is as profitable as any first entry, so the long cooldown
was leaving second/third legs on the table. With the flow hold-through, shrinking
the cooldown to 20–25m keeps per-trade EV flat (robust to ex-top-3-days) and
~doubles total captured PnL. This is NOT the old rejected relay: that was a blind
+2h re-entry (−0.14%); this re-enters only on a real fresh −7% break.

> The absolute EVs above use a full-timeline entry detector and idealized
> exit-then-reenter fills; the very-short-cooldown edge is exactly where a
> backtest is most optimistic (rapid re-fill slippage on a fast dump). Treat the
> per-trade numbers as directional and validate in live paper. Set
> `exit_flow_gate_enabled: false` and `same_symbol_cooldown_hours: 6` to fall
> back to the original E1 + 6h behavior for an A/B.

## Historical label stats and current exit audit

- naked label + E1: **3.28 trades/day, 67% win, +0.40%/trade, PF 1.21**,
  train/verdict near-zero decay.
- **near-book order-flow tier (LIVE-ready, wired)**: confirm the short when the
  top-20 book does NOT stack bids over the last 2m (imbalance delta ≤ 0). Near-bid
  laddering during a −7% break is a knife-catch → bounce → worse short, so this
  is the OPPOSITE sign to what boosts codex's core5_agg (same shared cache, own
  sign). Champion-label backtest: **train PF 1.40 / verdict PF 1.49, win 69–71%,
  ~1 trade/day**. See "BookDepth near-book tier" below.
- with far-depth gate (skip when 30s book far-side −3~−5% thickens ≥5%):
  **+0.64%/trade, PF 1.36, 1.76/day** — stronger per-trade but needs a DEEPER
  live snapshot (limit≥500) than the current collector's `limit=20`, and adds
  nothing in the train split (verdict-only gain). Deferred; the near-book tier is
  the live enhancement.
- agg sell-pressure gates (which help core5_agg) and agg/sustained-trigger early
  entry were tested and **do not help** this label — the deep-waterfall label
  already selects violent selling, and 59% of intra-minute breaks are wicks that
  only resolve at the 1m close. Kept off.

The label studies above remain useful for entry selection. They do not validate
the current flow-gated exit. The authoritative current exit audit is PF 0.958
on the same fixed entry path after executable-price correction.

## Config

`backend/config/settings.json → claude_board_waterfall`:

```json
{
  "enabled": true,
  "paper_initial_balance_usdt": 100.0,
  "paper_margin_fraction": 0.2,
  "leverage": 10.0,
  "max_open_positions": 5,
  "fee_rate": 0.0008,
  "min_ret_24h": 0.40,
  "break_window_min": 60,
  "break_drop": 0.07,
  "min_qv60_usdt": 300000.0,
  "stop_bounce_buffer": 0.01,
  "stop_min_pct": 0.015,
  "trail_activate": 0.035,
  "trail_rebound": 0.030,
  "max_hold_min": 240,
  "same_symbol_cooldown_hours": 0.3333,
  "max_trades_per_symbol_day": 8,
  "exit_flow_gate_enabled": true,
  "exit_flow_window": 10,
  "exit_flow_sell_threshold": 0.48,
  "paper_account_backfill_from": "2026-07-13T07:37:00+08:00",
  "paper_accounts": ["20% fixed", "10% fixed", "10% drawdown ladder"]
}
```

- `fee_rate` (0.08% round trip) is the exchange fee only.
- **Execution model**: entries and exits are modeled as **market orders** (a
  waterfall needs immediate fills; a resting limit risks non-fills). Fills cross
  the spread adversely by `slippage_bps` per side (default 10 = 0.10%), which
  also folds in the sub-second latency between the 1m-close signal and the fill
  (during a fast dump the price drifts against a short). Entry fills below the
  signal close, exits fill above the trigger. Total realistic round trip ≈ fee
  0.08% + slippage 0.20% = ~0.28%, matching the 0.30% research cost. Set
  `slippage_bps` to 0 for the old idealized fills.
- Turn off with `"enabled": false` and restart monitor.
- Paper-only: shares the same `WaterfallExecutionAdapter`, never places live
  orders.
- **Memory**: this engine now owns the single `Candle(slots=True)` deque store;
  disabling core5 removes the duplicate strategy lifecycle and aggTrade stream.

## BookDepth near-book tier

A non-destructive order-flow enhancement on this engine's own account, reusing
the SAME live cache the codex micro-collector publishes
(`storage/micro/latest_depth.json`, top-20 book imbalance + 2m baseline) — zero
new data infrastructure. The board engine reads that cache with its own sign
(`DepthSignalCache(confirm_direction="ask_heavy")`): confirm when the near book
is NOT becoming bid-heavy (`imbalance_delta_2m ≤ bookdepth_imbalance_delta_max`,
default 0.0), i.e. sellers stay in control. This is deliberately the opposite of
codex's `bid_heavy` sign — the same feature is inverted across the two
populations (recurring "the population decides whether a feature works" law).

Behaviour:

- **non-destructive by default** (`bookdepth_filter_mode: false`): every label
  entry still fires. A depth-confirmed entry is tagged `tier="depth_confirmed"`
  (`[Claude·冠军标签] … 档位 深度确认` in WeCom) and gets a
  `bookdepth_confidence_boost` (default +0.10); unconfirmed entries stay
  `tier="normal"`. This preserves the clean +0.40%/PF1.21 baseline while paper
  collects live evidence that the confirmed sub-tier really runs at PF≈1.49.
- **fail-open**: missing / stale / baseline-unready depth never blocks an entry
  (`bookdepth=bookdepth_missing|stale|baseline_unready` in evidence). The tier
  only ever activates when a fresh (≤75s) snapshot with a valid 90–210s baseline
  exists for the symbol — which requires the `collect-micro` service running.
- **hard-filter upgrade path**: set `bookdepth_filter_mode: true` to only open
  depth-confirmed entries (captures the PF lift, ~1 trade/day, but drops the
  marginal-winner trades). Recommended only after a live A/B confirms the
  sub-tier split.

Config lives in `settings.json → claude_board_waterfall` (`bookdepth_*`). Turn
the whole tier off with `bookdepth_enhancement_enabled: false`.

## Where it shows

- WeCom pushes once per master open/exit and lists all three account changes.
- `/waterfall`: separate 20% fixed, 10% fixed and 10% drawdown-ladder cards,
  plus one shared master position/signal stream.
- API: `/api/hunter/waterfall/positions?strategy=claude_board_wf_1m`,
  `.../signals?strategy=...`, and `accounts[]` in `/api/hunter/waterfall/summary`.

## State isolation and restart recovery

- Only Board positions restore into the live engine. Historical core5 rows are
  preserved but excluded from execution and default API queries.
- Closed-position history is restored without the old 1000-row cap, so paper
  equity does not lose early realized PnL after a long-running restart.
- Position sizing is capped by free paper equity. No position is created when
  equity or free margin is zero.
- On startup the three ledgers deterministically replay Board master trades from
  2026-07-13 07:37 CST. The dashboard total starts at 300U and each account is
  available separately in `accounts[]`.

## Reproducible production-engine replay

The repository includes a replay that imports `BoardWaterfallEngine` directly
and merges all selected symbols in timestamp order. This preserves global
position limits, account equity, margin use and cooldown behavior:

```bash
python backend/ml_experiments/backtest_board_waterfall.py \
  --klines-dir "E:\\A\\bb\\data\\klines" \
  --start 2026-01-01 --end 2026-06-30 \
  --split-date 2026-04-01
```

Use `--symbols NOMUSDT,LABUSDT` for a focused replay or `--max-symbols 50` for
a smoke run. Output includes the trade ledger plus all/train/holdout metrics:
frequency, win rate, average and median return, PF, MAE/MFE, 3%/5% winners and
PF with the largest winner removed. Positions still open at the end are
reported but are not force-closed into results.

The headline 2026H1 figures above predate this in-repository runner. Treat them
as research evidence until regenerated from the exact local dataset and saved
report; the production-engine replay is now the authoritative verification
path.

## Next (post paper A/B)

1. Watch the live `depth_confirmed` vs `normal` sub-tier split; once it confirms
   the backtest PF gap, flip `bookdepth_filter_mode` on (or add the deeper
   far-book gate via a `limit≥500` snapshot) — both need `collect-micro` live.
2. Per-shape gates (distribution-relay vs vertical-spike) — spike form is 50%
   wicks, the most dangerous; researched, not yet wired.
3. Tick-level exit (flow still hot vs bid returned) needs the live liquidation
   stream — the only untested lever on big-meat capture.
