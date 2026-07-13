# Dual Waterfall Runtime Fix - 2026-07-13

Commit: `cf1632b`

## Scope

This release corrects dual-engine restart recovery, dashboard account totals,
REST-ban fallback and zero-equity paper sizing. It also adds an exact production
Board Waterfall replay and regression tests.

## Corrected behavior

### Strategy state isolation

Before this release, both engines loaded global active positions and recent
history. A core5 engine could therefore receive a Board position whose
`exit_profile` was `claude_e1`, then fail on the next candle because core5 does
not own that profile. Board PnL and cooldown state could also contaminate the
core account.

Recovery now filters at both boundaries:

1. SQLite methods accept and apply `strategy`.
2. Each engine defensively rejects rows whose strategy id does not match.

The monitor restores all closed positions for realized PnL rather than only the
latest 1000 rows.

### Account aggregation

The dashboard previously combined the activity and PnL of two accounts but
added only one 100 USDT initial balance. Two accounts at 110 USDT each could be
shown as 120 instead of 220.

The API now builds the top-level total by summing both complete account
summaries. With default configuration:

```text
core5 initial       100 USDT
Board initial       100 USDT
combined initial    200 USDT
```

Win rate and average PnL are weighted by each account's closed trade count.

### Strict DB-only fallback

When exchange-info/ticker REST fails, the monitor now:

1. reads known 1m symbols from SQLite;
2. includes every active paper position;
3. primes engines from cached candles only;
4. does not call per-symbol Binance kline REST in that fallback pass.

Normal REST prewarm remains DB-first and weight-throttled.

### Board capital guard

Board position sizing now subtracts margin used by open positions and caps new
margin by free equity. Entry is rejected when margin or notional is zero.

## Reproducible replay

`backend/ml_experiments/backtest_board_waterfall.py` imports the production
engine directly and merges symbol candles by close time. It does not duplicate
the strategy rules. Example:

```bash
python backend/ml_experiments/backtest_board_waterfall.py \
  --klines-dir "E:\\A\\bb\\data\\klines" \
  --start 2026-01-01 --end 2026-06-30 \
  --split-date 2026-04-01
```

Open positions at the end are reported separately and are not force-closed into
performance statistics.

## Verification completed

- Backend: `52 passed`.
- Frontend: Next.js production build passed.
- Board replay: local parquet smoke replay passed.
- GitHub main: `cf1632b` was pushed before this documentation follow-up.

Regression coverage is in
`backend/tests/test_waterfall_dual_strategy.py` and includes:

- core/Board restart isolation;
- strategy-filtered and unlimited history retrieval;
- two-account aggregation;
- strict DB-only prewarm;
- REST universe failure routing;
- zero-equity position sizing;
- Board entry and trailing-profit exit lifecycle.

## Server update and checks

```bash
cd /opt/binance-hunter
sudo bash deploy/update.sh
python3 deploy/verify-live.py
```

The verifier requires both account strategy ids and a combined 200 USDT initial
balance. After restart, also inspect:

```bash
journalctl -u binance-hunter-monitor -n 80 --no-pager
curl -s http://127.0.0.1:8787/api/waterfall/summary | python3 -m json.tool
```
