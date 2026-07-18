# Claude Champion Paper Accounts

Production uses one `claude_board_wf_1m` signal and position lifecycle. Three
independent paper ledgers apply different sizing to that exact same path:

| Account id | Initial | Margin rule | Leverage |
| --- | ---: | --- | ---: |
| `claude_fixed20` | 100U | fixed 20% of realized equity | 10x |
| `claude_fixed10` | 100U | fixed 10% of realized equity | 10x |
| `claude_drawdown10` | 100U | 10% base with realized-drawdown ladder | 10x |

The ladder compares current realized equity with its historical realized peak:

- drawdown below 5%: factor 1.00, margin 10%;
- drawdown below 10%: factor 0.75, margin 7.5%;
- drawdown below 15%: factor 0.50, margin 5%;
- drawdown at least 15%: factor 0.25, margin 2.5%.

Floating PnL never changes the ladder tier. Sizing uses realized equity and
subtracts margin committed to concurrent open positions.

## Backfill and restart

`paper_account_backfill_from` is `2026-07-13T07:37:00+08:00`. At monitor
startup, all Claude master positions from that point are replayed in event-time
order. Exits are processed before entries sharing a timestamp so released
margin is available to the new trade. The derived account tables are replaced
atomically, making restart replay idempotent and allowing the two new ledgers to
start as if they had followed the original signal stream from its first trade.

Master positions remain in `waterfall_positions`. Derived account state is in:

- `waterfall_paper_accounts`;
- `waterfall_account_positions`.

## Notifications and API

Only the master strategy emits a signal. A single WeCom message contains the
entry margin or exit PnL/equity for all three ledgers. The API returns one master
positions/signals stream and three entries in `waterfall/summary.accounts`.

Core5 is disabled through `waterfall_quant.enabled=false`. Historical core5
rows are retained for direct SQLite audit and excluded from the production API,
even when a caller supplies an old Core5 `strategy` query parameter.
