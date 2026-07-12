# Board-Waterfall Strategy (Claude·冠军标签)

The second waterfall engine, running in parallel with Codex's `core5_agg` on
the same 1m WebSocket stream, with its own independent 100U paper account.
Engine: `backend/pump_dump_hunter/board_waterfall.py`, strategy id
`claude_board_wf_1m`.

## Why a second engine

Three days of independent research (audit → label refinement → walk-forward)
converged on a label that overlaps only ~30% with core5_agg: core5_agg catches
"already-falling downtrend continuation" (no board requirement); this engine
catches "board-coin deep waterfalls". Complementary, not competing — combined
frequency ~4 trades/day.

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

## Exit (E1, winner of an 8-variant battle)

- **structure stop** at `B × 1.01` where B = highest price after the 60m low
  (min `entry × 1.015`). This is the user's "bounce back to origin = wick" rule.
- **trailing profit**: activate at MFE ≥ 3.5%, rebound 3.0%, prev-bar-confirmed
  (no same-bar lookahead).
- **time stop**: 240 minutes.

Variants that lost: fixed tight/wide trail, stall-exit, half-take-profit, tight
B, hold-4h, win-relay re-entry, flow-adaptive trail. Each "eat more" tweak bled
elsewhere — 14% big-meat capture is the 1m information-set ceiling.

Cooldown 6h per symbol; win-relay re-entry was rejected (bounce follows our
exits; +2h relay tested −0.14% over 1471 samples).

## Verdict-period stats (2026H1, honest 0.30% cost)

- naked label + E1: **3.28 trades/day, 67% win, +0.40%/trade, PF 1.21**,
  train/verdict near-zero decay.
- with far-depth gate (skip when 30s book far-side −3~−5% thickens ≥5%):
  **+0.64%/trade, PF 1.36, 1.76/day** — the gate needs live depth polling
  (see `micro-collector.md`), configurable after paper A/B.
- agg sell-pressure gates (which help core5_agg) were tested and **do not help**
  this label — the deep-waterfall label already selects violent selling, so the
  gate only cuts frequency. Kept off.

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
  "same_symbol_cooldown_hours": 6.0,
  "max_trades_per_symbol_day": 2
}
```

- `fee_rate` deliberately matches core5_agg's 0.08% for A/B parity; the honest
  research figure is 0.30% — deduct ~0.2pp/trade when judging real edge.
- Turn off with `"enabled": false` and restart monitor.
- Paper-only: shares the same `WaterfallExecutionAdapter`, never places live
  orders.

## Where it shows

- WeCom push title: `[Claude·冠军标签] 瀑布开空/止盈/止损 ...`
- `/waterfall`: an "独立账户 · Claude·冠军标签" card (equity/realized/
  unrealized/win-rate), plus per-strategy tags on position cards and signals.
- API: `/api/hunter/waterfall/positions?strategy=claude_board_wf_1m`,
  `.../signals?strategy=...`, and `accounts[]` in `/api/hunter/waterfall/summary`.

## Next (post paper A/B)

1. Enable the far-depth gate once collect-micro depth polling has a 2h baseline.
2. Per-shape gates (distribution-relay vs vertical-spike) — spike form is 50%
   wicks, the most dangerous; researched, not yet wired.
3. Tick-level exit (flow still hot vs bid returned) needs the live liquidation
   stream — the only untested lever on big-meat capture.
