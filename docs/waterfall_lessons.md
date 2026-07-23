# Waterfall Short Strategy — Research Lessons & Playbook

Written from the Claude `board_waterfall` engine's research trail so the codex
`core5_agg` engine (and any future waterfall work) can reuse the hard-won
lessons instead of re-paying for them. Everything below is backed by scripts and
a train/verdict + ex-top-days audit; where a claim is fragile it says so.

> **2026-07-24 execution audit:** the historical flow-gate headline in this
> document is superseded. Its replay filled a stale trailing trigger after the
> market had already crossed it. Executable-price correction changed the same
> 3,629-trade path from PF 1.565 to PF 0.958. Treat the flow-gate discussion
> below as research history, not production performance. See
> `docs/champion/04-止盈可执行性审计-20260724.md`.

---

## 0. TL;DR — the five lessons that would have saved the most time

1. **The population decides whether a feature works** (总体决定特征有效性). The
   same order-flow / order-book / OI feature helps one label and is useless — or
   *inverted* — on another. Never port a gate across labels without re-validating,
   and expect the sign to flip.
2. **A backtest number is guilty until proven robust.** Require: held-out verdict
   period, positive after removing the top 3 days, a monotonic (not peaked)
   parameter frontier, and a *mechanism*. In-sample tuning routinely turns
   +2.6%/trade into −0.3% out of sample here.
3. **Cost model is a strategy decision, not a footnote.** 0.08% fee-only is a lie
   for market orders on meme dumps; the honest figure is ~0.30% round trip
   (fee + slippage). Most "edges" die at 0.30%.
4. **Separate what the information set can and cannot predict.** The *top* is
   unpredictable from price/OI/funding (0.78 AUC ceiling, falsified many times).
   Wick-vs-real is unresolved until the 1m close. Knowing where the wall is stops
   you from grinding on impossible tasks.
5. **Weak AUC + large payoff asymmetry can still be a strong edge — test net EV,
   not AUC.** Our best exit improvement rests on a feature with AUC ≈ 0.55.

---

## 1. The dominating law: the population decides the feature

Every "why did the same idea help there and not here" surprise reduced to this.
Concrete cases from this project:

| Feature | Population A | Population B | Outcome |
| --- | --- | --- | --- |
| agg taker-sell entry gate | core5_agg (downtrend continuation) → **helps** (verdict −0.11%→+0.30%) | board label (+40%/−7% deep waterfall) → **useless** (AUC 0.43) | the deep label already pre-selects violent selling, so the gate only cuts frequency |
| OI direction into the break | old shallow population → OI already drained is the signal | board deep population → real waterfalls have OI *still rising* (longs trapped = fuel) | **opposite sign** |
| bookDepth near-book imbalance | codex core5_agg → confirm when book gets **bid-heavy** | board label → confirm when book is **NOT** bid-heavy (bids stacking = knife-catch = bounce) | **opposite sign**, both validated on their own label |

Implication for codex: when you see a Claude-side gate that works, do not copy the
threshold or even the sign. Re-fit on your own label, and if the mechanism is
"who is winning the order flow", the sign may well invert.

---

## 2. Backtest traps we actually hit (each cost real time)

- **In-sample selection bias.** Scoring signals with a model on its own training
  window, or sweeping TP/SL on the data you selected on. `bt_reconcile`: TP8/SL10
  looked like +2.61%/trade, 70% win in-sample → **−0.33%/trade, 53% win** on a
  true walk-forward. Absolute numbers from any in-sample sweep are inflated;
  only the *relative ordering* survives.
- **Top-N-days dependence.** A cascade day (2025-10-10, 2025-05-19, an FTX-like
  event) can carry an entire "edge". Always report the metric again after
  removing the best ~3 days. Several "+1.9%/trade" results collapsed to ≈0.
- **Multiple comparisons.** 276 filters ranked on 55 trades produced a "+2.89%"
  winner whose permutation-test P(noise ≥ observed) = 0.407 — indistinguishable
  from luck. If you grid-search filters, run a permutation test.
- **Short-PnL formula.** `entry/exit − 1` is coin-margined and overstates the fat
  tail ~5×. Linear USDT short PnL is `1 − exit/entry`. A single crash trade got
  logged as +46% under the wrong formula.
- **Survivorship bias in early-entry samples.** Testing "enter at 40s" only on
  breaks that were confirmed at the close hides the wicks that recovered by 59s —
  the exact trades early entry loses on. Sample the *raw* intra-minute breaks.
- **pandas 2.x timestamp bug (hit 3×).** `pd.to_datetime(str).astype('int64')`
  can return **microseconds**, not nanoseconds; `//10**6` then yields seconds and
  silently misaligns everything by 1000×. Go via `.astype('datetime64[ms]')`.
- **Loop-guard truncation.** A `while … guard < 100000` cap silently truncated
  long-history symbols to their first ~70 days and **inflated per-trade EV** (a
  reported +2.5%/trade was really +1.5% on the full timeline). Any early-exit in a
  scan must be logged, never silent.

---

## 3. The robustness protocol (what we require before believing anything)

1. **Select / verdict split** — tune on 2023–2025, judge only on 2026H1. Near-zero
   decay between them is the pass condition.
2. **Ex-top-3-days** — the metric must stay positive after removing the 3 biggest
   trade-days of the verdict period.
3. **Monthly positive fraction** — how many verdict months are individually
   positive (100% is a strong signal, 60% is fragile).
4. **Monotone, not peaked** — a good parameter should sit on a smooth frontier; a
   lone spiking cell is overfit. We pick the *shoulder*, not the peak.
5. **Mechanism first** — if there is no story for *why* the market pays this, it is
   probably a fit. AUC alone never clears a gate.

---

## 4. Label design — the champion label

Path: v1 rules → v2 (`new_high_reset`, weak-close breakdown) → two-stage ML
(candidate rule → LightGBM) → **the board-coin waterfall label**, which is the
user's own manual recipe, quantified:

- **board coin**: 24h return ≥ +40%
- **detection**: 1m close ≤ 60m rolling high × (1 − 7%)
- **liquidity**: 60m quote volume ≥ 300k USDT

Derived from a 27-definition sweep where both drop depth (3/5/7%) and board
threshold (10/20/40%) were **monotonic**; the +40%/−7% corner is the highest-EV,
highest-purity point (**49% base true-waterfall rate vs 32%** for the old label).
Lesson: define the *population* deliberately — the "five features have no
discriminative power" verdict was only ever true of the *old* population; on the
right population a single price feature (`bounce_in` = distance already rebounded
from the 60m low) reaches AUC 0.70.

---

## 5. Entry timing — why we wait for the 1m close

Tick-early entry was tested three independent ways and **rejected mechanistically**
(not for sample size):

- **59% of intra-minute −7% breaks are wicks** that close back above. No rich
  confirmation feature separates real from wick: monotonicity AUC 0.57 is the
  best; intensity (0.395) and depth (0.414) are **inverted** — the more violent /
  deeper the break, the more likely it is a wick. Any "confirm on violence" gate
  selects *for* wicks.
- **Aggregating the trigger** (require the break to hold K seconds) lowers the
  wick rate (59%→29% at K=12s) but does **not** improve return (−2.3% at every K)
  — because the loss is the *bounce tax*, not the *wick tax*: after any break the
  price mean-reverts toward the close, so entering at the spike-bottom is a bad
  short regardless. Only the full 60s close lets the intra-minute bounce settle.
- So the 1m close is not an arbitrary delay — it is the point where wick-vs-real
  resolves and the intra-minute mean-reversion has played out.

---

## 6. Exit design — the biggest single win

### 6.1 The base (E1) and the ceiling we thought we hit

E1 won an 8-variant battle: **structure stop** at `B×1.01` (B = highest price
after the 60m low; the "bounce back to origin = wick" rule), **trailing profit**
(activate MFE≥3.5%, rebound 3.0%, prev-bar-confirmed), **240-min timeout**. The
losers (fixed/wide trail, stall-exit, half-TP, tight-B, hold-4h, blind relay,
flow-adaptive trail) each bled elsewhere, and we wrote down "big-meat capture =
14% = the 1m information-set ceiling, case closed." **That conclusion was too
strong** — see 6.3.

### 6.2 The prize (why the ceiling was worth re-attacking)

E1 exits on the *first* bounce. On the champion label, **94% of big-meat trades
make a new low AFTER E1 exits** (median −13% further; super-meat median −31.7%).
The meat is real and enormous; the only question was whether the fake bounce
(temporary) can be told from the real bounce (trend over) at the exit moment.

### 6.3 The discriminator: bookDepth NO, taker-sell flow YES

Head-to-head at the exit moment on 1024 trades with full sub-minute data:

- **bookDepth does NOT separate fake/real bounces** — near-book imbalance, its
  2-min delta, far-book thickening, bid/ask totals: **all AUC ≈ 0.5**. The order
  book at the bounce says nothing about whether the dump resumes.
- **1m taker-sell flow does** — weakly (exit-bar sell-ratio AUC ≈ 0.55), but the
  direction is right: if sellers still dominate as the price bounces, the bounce
  is fake.

### 6.4 The winning design: flow-gated hold-through

Skip the trailing take-profit when taker-sell over the prior W=10 closed bars is
still ≥ θ=0.48 (sellers in control ⇒ hold for the next leg). **Keep the
B-structure stop always active** as the backstop, so a held trade can never lose
more than E1. Backtest (full timeline, train/verdict + ex-top-3-days robust):
**~+1.44%/trade at 61% win vs E1's ~+0.19%** on the same detector; the gain is
entirely in the big/super-meat tiers, small/mid meat untouched.

Two lessons here that generalize:

- **Weak AUC (0.55) + huge payoff asymmetry = strong net EV.** We almost dismissed
  this on AUC; the net-EV simulation (which respects the asymmetry) is what
  revealed it. Test the P&L, not the classifier.
- **Why e8 (flow-adaptive trail) failed but this works.** e8 added an *early exit*
  on "flow died", which fired during mid-cascade pauses and cut winners. This gate
  only ever *delays* the take-profit and never adds an early exit, so it cannot
  hurt losers or small winners. When using flow at the exit, delay — don't cut.
- **W has an optimum, longer is not better.** W=3→10 improves, W=16/20 degrades
  (win rate falls to 47%): too long a "still selling" window holds through real
  reversals too. θ 0.46–0.48 is the shoulder; 0.50 at W=10 over-tightens.

### 6.5 Cooldown / second-waterfall relay

- **Blind +2h relay was rejected** (−0.14% over 1471 samples) — re-entering on a
  timer catches the bounce that follows our own exit.
- **Fresh-break relay works.** Shortening the same-symbol cooldown 6h → 20m (with
  the flow hold-through exit) keeps per-trade EV flat (robust to ex-top-days) and
  **~doubles total captured PnL** — a genuine second +40%/−7% waterfall is as
  profitable as any first entry. The distinction is structural: re-enter only on a
  real new break, never on a timer. 20–25m is the shoulder; 0-cooldown adds a
  little total PnL but starts to dilute per-trade and concentrates risk.

> Honesty caveat carried into production: the very-short-cooldown edge uses
> idealized exit-then-reenter fills; that is exactly where a backtest is most
> optimistic (rapid re-fill slippage on a fast dump). It ships behind a flag and
> must be confirmed in live paper.

---

## 7. Enhancements — wired, deferred, killed

- **bookDepth near-book entry tier** — wired, non-destructive (tags
  `depth_confirmed` + a confidence bump, fail-open), reuses the codex collector's
  cache with the board label's *inverted* sign. Dormant until `collect-micro`
  publishes depth.
- **Far-book (−3~−5%) entry gate** — validated (verdict PF 1.21→1.36) but needs a
  `limit≥500` depth snapshot the collector doesn't take yet. Deferred.
- **OI confirmation gate** — inconsistent across train/verdict; dropped.
- **agg sell-pressure entry gate** — useless on this label (population law).

---

## 8. Information-set ceilings (do not grind here)

- **The top is unpredictable** from price + funding + OI + order flow (AUC ~0.78
  ceiling; long-context, OI/funding, 365d-vs-900d training all pinned at ~0.78).
  Top signals have value only as *exit/close-long* alerts, never as short entries
  (top-short: 80% of "tops" are broken by >5%, adverse median 19.7%).
- **Wick-vs-real is unresolved until the 1m close** (Section 5).
- **Fake-vs-real bounce is invisible to the order book** at the exit moment
  (Section 6.3); only flow gives a weak edge.

These are information-set limits — more model or more data will not move them.
The only untested lever that *could* is genuine sub-second data (live liquidation
stream, real-time OI), which is why `collect-micro` exists.

---

## 9. Direct notes for codex's core5_agg

1. **Re-run the flow-gated hold-through on your label.** Your downtrend-
   continuation population is different from the board deep-waterfall population,
   so expect a different θ and possibly a different W — but the *shape* (delay the
   take-profit while sellers dominate, keep the stop hard) should transfer. Do the
   net-EV test, not just AUC.
2. **The relay/cooldown lesson likely transfers** — re-enter on a fresh structural
   trigger, not a timer; sweep the cooldown with the ex-top-days guard.
3. **Adopt the honest cost model (0.30%)** in any A/B; your paper PnL at 0.08% is
   ~0.2pp/trade optimistic per trade.
4. **Do not copy Claude-side gate signs.** bookDepth and OI both invert between our
   populations. Re-fit.
5. **Watch the four bugs**: coin-margined PnL formula, pandas µs timestamps, silent
   loop-guard truncation, and in-sample filter selection (permutation-test it).

---

*Engines: `board_waterfall.py` (Claude champion label) and `waterfall.py`
(codex core5_agg) run in parallel on one 1m stream with independent paper
accounts. See `board_waterfall.md` and `waterfall_quant.md` for the live config.*
