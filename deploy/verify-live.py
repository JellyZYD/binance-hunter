#!/usr/bin/env python3
"""Verify that the live server is running the expected lifecycle router build."""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


BASE_URL = os.environ.get("HUNTER_VERIFY_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
API_PREFIX = "/" + os.environ.get("HUNTER_VERIFY_API_PREFIX", "/api").strip("/")
EXPECTED_STRATEGY = os.environ.get("HUNTER_EXPECTED_STRATEGY", "lifecycle_router_expert")
TRIES = int(os.environ.get("HUNTER_VERIFY_TRIES", "20"))
SLEEP_SECONDS = float(os.environ.get("HUNTER_VERIFY_SLEEP", "1.5"))


def fetch_json(name: str) -> dict:
    with urlopen(f"{BASE_URL}{API_PREFIX}/{name.lstrip('/')}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    last_error: Exception | None = None
    summary: dict | None = None
    model: dict | None = None
    for _ in range(max(1, TRIES)):
        try:
            summary = fetch_json("summary")
            model = fetch_json("model")
            break
        except Exception as exc:  # noqa: BLE001 - deployment verification should report any startup failure.
            last_error = exc
            time.sleep(SLEEP_SECONDS)

    if summary is None or model is None:
        print(f"live verify failed: API not reachable at {BASE_URL}: {last_error}", file=sys.stderr)
        return 1

    strategy = (summary.get("strategy") or {}).get("strategy_version")
    lifecycle = model.get("lifecycle") or {}
    lifecycle_strategy = lifecycle.get("strategy_version")
    router_meta = lifecycle.get("route") or {}
    router_model = router_meta.get("model")
    summary_strategy = summary.get("strategy") or {}
    runtime = lifecycle.get("runtime") or {}
    high_pump = lifecycle.get("high_pump") or {}

    print(
        "live "
        f"strategy={strategy} lifecycle_model={lifecycle_strategy} router={router_model} "
        f"pump_min_gain={runtime.get('pump_signal_min_gain_pct')} "
        f"high_pump={runtime.get('high_pump_enabled')} min_gain={runtime.get('high_pump_min_gain_pct')}"
    )
    if strategy != EXPECTED_STRATEGY:
        print(f"live verify failed: expected strategy_version={EXPECTED_STRATEGY}, got {strategy}", file=sys.stderr)
        return 1
    if lifecycle_strategy != EXPECTED_STRATEGY:
        print(
            f"live verify failed: expected lifecycle model strategy={EXPECTED_STRATEGY}, got {lifecycle_strategy}",
            file=sys.stderr,
        )
        return 1
    if router_model != "family_router":
        print(f"live verify failed: family router model missing: {router_meta}", file=sys.stderr)
        return 1
    if summary_strategy.get("lifecycle_high_pump_enabled") is not True:
        print(f"live verify failed: high-pump not enabled in summary strategy: {summary_strategy}", file=sys.stderr)
        return 1
    if abs(float(summary_strategy.get("lifecycle_pump_signal_min_gain_pct", 0) or 0) - 25.0) > 0.01:
        print(f"live verify failed: formal PumpWatch min gain is not 25% in summary: {summary_strategy}", file=sys.stderr)
        return 1
    if abs(float(runtime.get("pump_signal_min_gain_pct", 0) or 0) - 25.0) > 0.01:
        print(f"live verify failed: lifecycle runtime PumpWatch min gain is not 25%: {runtime}", file=sys.stderr)
        return 1
    if runtime.get("high_pump_enabled") is not True or float(runtime.get("high_pump_min_gain_pct", 0) or 0) < 40:
        print(f"live verify failed: high-pump runtime missing: {runtime}", file=sys.stderr)
        return 1
    if not high_pump or "high_top" not in (lifecycle.get("models") or {}):
        print(f"live verify failed: high-pump model metadata missing: {high_pump}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
