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
EXPECTED_STRATEGY = os.environ.get("HUNTER_EXPECTED_STRATEGY", "waterfall_quant")
EXPECTED_VARIANT = os.environ.get("HUNTER_EXPECTED_WATERFALL_VARIANT", "core5_agg")
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
    required_families = {"post_pump", "downtrend_continuation", "other"}

    print(
        "live "
        f"strategy={active} variant={variant} broad_top={cfg.get('broad_top')} "
        f"interval={cfg.get('watch_interval')} micro={cfg.get('micro_streams')} "
        f"paper={cfg.get('paper_initial_balance_usdt')}U "
        f"margin={cfg.get('paper_margin_fraction')} leverage={cfg.get('leverage')} "
        f"agg={cfg.get('require_agg_confirmation')}"
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
    if families != required_families:
        print(f"live verify failed: enabled_families={sorted(families)}", file=sys.stderr)
        return 1
    if cfg.get("require_agg_confirmation") is not True or "aggTrade" not in (cfg.get("micro_streams") or []):
        print(f"live verify failed: aggTrade confirmation not active: {cfg}", file=sys.stderr)
        return 1
    if cfg.get("execution_mode") != "paper" or cfg.get("real_order_enabled") is not False:
        print(f"live verify failed: execution must remain paper-only before real adapter review: {cfg}", file=sys.stderr)
        return 1
    if abs(float(cfg.get("paper_initial_balance_usdt") or 0.0) - 100.0) > 0.01:
        print(f"live verify failed: paper initial balance must be 100U: {cfg}", file=sys.stderr)
        return 1
    if abs(float(cfg.get("paper_margin_fraction") or 0.0) - 0.2) > 0.001:
        print(f"live verify failed: paper margin fraction must be 20%: {cfg}", file=sys.stderr)
        return 1
    if abs(float(cfg.get("leverage") or 0.0) - 10.0) > 0.001:
        print(f"live verify failed: leverage must be 10x: {cfg}", file=sys.stderr)
        return 1
    accounts = summary.get("accounts") or []
    account_strategies = {str(account.get("strategy") or "") for account in accounts}
    expected_accounts = {f"waterfall_{EXPECTED_VARIANT}_1m", "claude_board_wf_1m"}
    if account_strategies != expected_accounts:
        print(f"live verify failed: independent accounts={sorted(account_strategies)}", file=sys.stderr)
        return 1
    account_initial = sum(float(account.get("paper_initial_balance_usdt") or 0.0) for account in accounts)
    total_initial = float(summary.get("paper_initial_balance_usdt") or 0.0)
    if abs(total_initial - account_initial) > 0.01 or abs(total_initial - 200.0) > 0.01:
        print(
            f"live verify failed: combined initial={total_initial} account sum={account_initial}, expected 200U",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
