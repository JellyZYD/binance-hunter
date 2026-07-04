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

    print(f"live strategy={strategy} lifecycle_model={lifecycle_strategy} router={router_model}")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
