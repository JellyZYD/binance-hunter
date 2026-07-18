#!/usr/bin/env python3
"""Verify that the live server is running the waterfall quant strategy."""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.request import urlopen


BASE_URL = os.environ.get("HUNTER_VERIFY_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
API_PREFIX = "/" + os.environ.get("HUNTER_VERIFY_API_PREFIX", "/api").strip("/")
EXPECTED_STRATEGY = os.environ.get("HUNTER_EXPECTED_STRATEGY", "claude_board_wf_1m")
EXPECTED_VARIANT = os.environ.get("HUNTER_EXPECTED_WATERFALL_VARIANT", "claude_champion_three_accounts")
EXPECTED_ACCOUNTS = {"claude_fixed20", "claude_fixed10", "claude_drawdown10"}
TRIES = int(os.environ.get("HUNTER_VERIFY_TRIES", "20"))
SLEEP_SECONDS = float(os.environ.get("HUNTER_VERIFY_SLEEP", "1.5"))


def fetch_json(name: str) -> dict:
    with urlopen(f"{BASE_URL}{API_PREFIX}/{name.lstrip('/')}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    last_error: Exception | None = None
    summary: dict | None = None
    for _ in range(max(1, TRIES)):
        try:
            summary = fetch_json("waterfall/summary")
            cfg = summary.get("config") or {}
            account_ids = {
                str(account.get("account_id") or "")
                for account in (summary.get("accounts") or [])
            }
            if (
                summary.get("active_strategy") != EXPECTED_STRATEGY
                or cfg.get("variant") != EXPECTED_VARIANT
                or account_ids != EXPECTED_ACCOUNTS
            ):
                raise RuntimeError(
                    f"strategy/account migration not ready: strategy={summary.get('active_strategy')} "
                    f"variant={cfg.get('variant')} accounts={sorted(account_ids)}"
                )
            break
        except Exception as exc:  # noqa: BLE001 - deployment verification should report startup failures.
            last_error = exc
            time.sleep(SLEEP_SECONDS)

    if summary is None:
        print(f"live verify failed: waterfall API not reachable at {BASE_URL}: {last_error}", file=sys.stderr)
        return 1

    cfg = summary.get("config") or {}
    active = summary.get("active_strategy")
    variant = cfg.get("variant")
    families = set(cfg.get("enabled_families") or [])

    print(
        "live "
        f"strategy={active} variant={variant} broad_top={cfg.get('broad_top')} "
        f"interval={cfg.get('watch_interval')} micro={cfg.get('micro_streams')} "
        f"paper={cfg.get('paper_initial_balance_usdt')}U "
        f"margin={cfg.get('paper_margin_fraction')} leverage={cfg.get('leverage')} "
        f"accounts={cfg.get('account_count')} core5={cfg.get('core5_enabled')} "
        f"bookdepth={cfg.get('bookdepth_enhancement_enabled')}"
    )
    if active != EXPECTED_STRATEGY:
        print(f"live verify failed: expected active_strategy={EXPECTED_STRATEGY}, got {active}", file=sys.stderr)
        return 1
    if variant != EXPECTED_VARIANT:
        print(f"live verify failed: expected variant={EXPECTED_VARIANT}, got {variant}", file=sys.stderr)
        return 1
    if cfg.get("watch_interval") != "1m":
        print(f"live verify failed: expected 1m watch interval, got {cfg.get('watch_interval')}", file=sys.stderr)
        return 1
    if int(cfg.get("broad_top") or 0) < 300:
        print(f"live verify failed: broad_top too small: {cfg.get('broad_top')}", file=sys.stderr)
        return 1
    if families != {"board_waterfall"}:
        print(f"live verify failed: enabled_families={sorted(families)}", file=sys.stderr)
        return 1
    if cfg.get("core5_enabled") is not False:
        print(f"live verify failed: retired core5 engine is still enabled: {cfg}", file=sys.stderr)
        return 1
    if cfg.get("micro_streams"):
        print(f"live verify failed: Claude-only monitor has unexpected micro streams: {cfg.get('micro_streams')}", file=sys.stderr)
        return 1
    if cfg.get("account_count") != 3:
        print(f"live verify failed: expected three accounts: {cfg.get('account_count')}", file=sys.stderr)
        return 1
    if cfg.get("backfill_from") != "2026-07-13T07:37:00+08:00":
        print(f"live verify failed: unexpected account backfill start: {cfg.get('backfill_from')}", file=sys.stderr)
        return 1
    if cfg.get("bookdepth_enhancement_enabled") is not True:
        print(f"live verify failed: BookDepth enhancement not active: {cfg}", file=sys.stderr)
        return 1
    if cfg.get("execution_mode") != "paper" or cfg.get("real_order_enabled") is not False:
        print(f"live verify failed: execution must remain paper-only before real adapter review: {cfg}", file=sys.stderr)
        return 1
    if abs(float(cfg.get("paper_initial_balance_usdt") or 0.0) - 100.0) > 0.01:
        print(f"live verify failed: paper initial balance must be 100U: {cfg}", file=sys.stderr)
        return 1
    accounts = summary.get("accounts") or []
    by_id = {str(account.get("account_id") or ""): account for account in accounts}
    if set(by_id) != EXPECTED_ACCOUNTS:
        print(f"live verify failed: independent accounts={sorted(by_id)}", file=sys.stderr)
        return 1
    if {str(account.get("strategy") or "") for account in accounts} != {EXPECTED_STRATEGY}:
        print("live verify failed: all accounts must share the Claude signal strategy", file=sys.stderr)
        return 1
    expected_fractions = {"claude_fixed20": 0.2, "claude_fixed10": 0.1, "claude_drawdown10": 0.1}
    for account_id, fraction in expected_fractions.items():
        account = by_id[account_id]
        if abs(float(account.get("paper_initial_balance_usdt") or 0.0) - 100.0) > 0.01:
            print(f"live verify failed: {account_id} initial balance is not 100U", file=sys.stderr)
            return 1
        if abs(float(account.get("base_margin_fraction") or 0.0) - fraction) > 0.001:
            print(f"live verify failed: {account_id} margin fraction={account.get('base_margin_fraction')}", file=sys.stderr)
            return 1
        if abs(float(account.get("leverage") or 0.0) - 10.0) > 0.001:
            print(f"live verify failed: {account_id} leverage={account.get('leverage')}", file=sys.stderr)
            return 1
    if by_id["claude_drawdown10"].get("sizing_mode") != "realized_drawdown_ladder":
        print("live verify failed: drawdown account ladder is not active", file=sys.stderr)
        return 1
    account_initial = sum(float(account.get("paper_initial_balance_usdt") or 0.0) for account in accounts)
    total_initial = float(summary.get("paper_initial_balance_usdt") or 0.0)
    if abs(total_initial - account_initial) > 0.01 or abs(total_initial - 300.0) > 0.01:
        print(
            f"live verify failed: combined initial={total_initial} account sum={account_initial}, expected 300U",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
