# Waterfall Quant Production Mode

This server mode runs the latest waterfall short strategies. The old
lifecycle/long-short monitor is not the default runtime anymore.

> **Two engines run in parallel** on the same 1m stream, each with its own
> independent 100U paper account (see `board_waterfall.md` for the Claude
> engine). This doc covers the **Codex core5_agg** engine. Both are labeled
> per strategy in pushes and on `/waterfall`.

## Active Runtime

`backend/config/settings.json`

```json
{
  "runtime": {
    "active_strategy": "waterfall_quant"
  }
}
```

`python backend/run.py monitor` now dispatches to `waterfall_monitor` when the
active strategy is `waterfall_quant`.

## Strategy

Production strategy: `core5_agg`.

The live path is deliberately two-stage:

1. REST refreshes the liquid USDT-M futures universe every 15 minutes.
2. WebSocket subscribes to each selected symbol's `1m kline` and `aggTrade`.
3. Closed `1m` candles detect the core waterfall structure.
4. The same minute's `aggTrade` flow confirms real sell pressure before entry.
5. Paper execution opens a simulated short and manages stop/trailing exits.

Direct partial-minute agg entries are not enabled. Same-window replay showed
they were faster but too noisy. aggTrade is used as a confirmation filter.

## Universe

Default server scan:

- broad liquidity universe: Top 450 USDT perpetual futures;
- excludes BTC/ETH/BNB/SOL/XRP/DOGE/ADA/TRX and stock/metal/gas contracts from
  the global exclude list;
- keeps active open paper positions subscribed even if they fall out of TopN.

For a 2C2G server, Top 300 is the conservative operating point. Top 450 is the
aggressive coverage setting currently configured.

## Enabled Families

Enabled in production:

- `post_pump`: waterfall after a large 24h/runup move.
- `downtrend_continuation`: already weak contracts flushing lower again.
- `other`: non-standard but historically tradable waterfall structures.

Disabled in production:

- `range_breakdown`: too many false positives without deeper order-book data.
- `momentum_dump`: kept in code and research, not enabled by default.

## Core5 + agg Filters

Closed 1m core rules check:

- quote volume floor;
- red body / 2m / 5m drop;
- volume expansion;
- taker sell share from the kline;
- body close below recent structure low;
- family-specific context filters.

aggTrade confirmation checks:

- normal post/other: `m0_59s_sell_ratio >= 0.60` and
  `m0_59s_low_time_frac >= 0.55`;
- downtrend continuation:
  `m0_40s_sell_ratio >= 0.64` and `m0_59s_low_time_frac >= 0.80`;
- strong tier: `m0_50s_sell_ratio >= 0.64` and
  `m0_50s_close_pos <= 0.15`.

The strong tier is low-frequency but historically much cleaner.

## Paper Execution

Default paper account:

- initial balance: 100 USDT;
- margin per new trade: 20% of current equity;
- leverage: 10x;
- max open positions: 5;
- fee assumption: 0.08% round trip (exchange fee);
- execution: market orders with `slippage_bps` one-sided slippage (default 10 =
  0.10%/side, latency folded in) — entry fills below the signal close, exit
  fills above the trigger, both adverse to the short. ~0.28% realistic round
  trip; set to 0 for idealized fills;
- same-symbol cooldown: 4h;
- after-stop cooldown: 6h;
- max trades per symbol per day: 2.

Each position stores:

- entry/mark/exit price;
- margin, notional and leverage;
- stop, best price, worst price and trailing price;
- realized PnL and margin ROI;
- rule, family, exit profile and evidence.

The dashboard shows account equity, free balance, used margin, realized PnL and
unrealized PnL.

## Push Messages

WeCom push is concise:

- action: open short / take profit / stop loss / timeout exit;
- symbol;
- price and stop;
- tier and confidence;
- family and rule;
- margin, notional, leverage or realized result.

No direct page links are pushed.

## Real Order Adapter

`WaterfallExecutionAdapter` is wired into the signal flow but remains paper-only
by default:

```json
{
  "execution_mode": "paper",
  "real_order_enabled": false
}
```

Changing to live mode without an implemented order adapter fails loudly. This
prevents accidental real orders before the Binance API order module is reviewed.

## Historical Evidence

The best same-window comparison so far:

| Strategy | Trades | Frequency | Win | PF | Avg PnL | Avg MAE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| closed 1m baseline | 14 | 2.00/day | 35.7% | 1.87 | +1.49% | 2.60% |
| direct agg early entry | 32 | 4.57/day | weak | 0.89 | -0.15% | high |
| 1m + agg fast stop | 14 | 2.00/day | 35.7% | 1.94 | +1.55% | 2.07% |
| 1m + agg sell filter | 10 | 1.43/day | 50.0% | 3.57 | +3.23% | 1.84% |
| strong agg tier | 4 | 0.57/day | 75.0% | 15.18 | +9.35% | 1.76% |

Direct strict scanning remains research-only until it beats core5+agg on the
same symbols and same dates.

> **Cost-model caveat**: the table above uses the optimistic 0.08% round-trip
> fee with no slippage. Under an honest 0.30% round-trip on the full-history
> independent replay, the core5+agg edge is roughly +0.30%/trade in the 2026H1
> verdict period (vs the +1.16% headline), and drops to ≈0 once the few
> cascade days (e.g. 2025-10-10) are removed. Paper PnL is shown at 0.08% for
> A/B parity with the Claude engine; mentally deduct ~0.2pp/trade when judging
> the real edge. See `审查意见_claude_round4_部署版.md` in the review pack.

## Commands

Run monitor:

```bash
cd /opt/binance-hunter
source backend/.venv/bin/activate
PYTHONPATH=backend python backend/run.py monitor
```

Manual waterfall command:

```bash
PYTHONPATH=backend python backend/run.py waterfall-monitor --broad-top 450 --discover-every 15m
```

Dashboard:

- `/` production waterfall dashboard;
- `/waterfall` same waterfall dashboard;
- frontend can run on Vercel (see `../deploy/README.md`), calling the server
  API via `HUNTER_API_BASE_URL=https://pixia.cc/hunter-api`.

## Health & Memory (2G box)

- `/api/system` exposes CPU/mem/disk/network, Binance stream health (1m candle
  freshness), data sizes, and the monitor process's own RSS/heartbeat (written
  every 30s to `storage/monitor_health.json`); shown on the `/waterfall` row.
- Candle memory is kept small by `Candle(slots=True)` + a single candle store
  shared between both engines (~80MB total); prewarm writes one watch row per
  symbol (not per candle). systemd `MemoryMax` is the hard OOM backstop.
- REST prewarm is weight-throttled (`rest_weight_per_sec`, default 20) so
  restarts and 15m refreshes never trip Binance's 2400/min ban; if the REST
  universe call is banned, the monitor falls back to the DB's known symbols
  and switches prewarm to strict DB-only mode. It does not issue per-symbol
  kline REST calls while the universe request is unavailable.
- Restart recovery is strategy-scoped. Each engine restores only its own open
  positions and complete realized-PnL history, preventing exit-profile,
  cooldown and account contamination between core5 and Board Waterfall.
- SQLite runs in WAL mode so the read-only API never blocks on candle writes.
- See `../deploy/README.md` → 内存与防死机 / 限流与重启 for the full playbook.
