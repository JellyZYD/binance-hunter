#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a private server-only live execution config.",
    )
    parser.add_argument("--base", default="backend/config/settings.json")
    parser.add_argument("--output", default="backend/config/live.server.json")
    parser.add_argument("--max-notional-usdt", type=float, default=21.0)
    args = parser.parse_args()

    source = Path(args.base).resolve()
    output = Path(args.output).resolve()
    settings = json.loads(source.read_text(encoding="utf-8"))
    live = settings.setdefault("live_trading", {})
    live.update({
        "enabled": True,
        "mode": "live",
        "real_order_enabled": True,
        "ledger_path": "storage/live_trading.db",
        "account_api": "portfolio_margin",
        "position_mode": "hedge",
        "leverage": 5,
        "isolated_margin": False,
        "max_open_positions": 3,
        "sizing_mode": "realized_drawdown_ladder",
        "base_margin_fraction": 0.20,
        "margin_fraction_cap": 0.20,
        "max_notional_usdt": float(args.max_notional_usdt),
        "signal_source": "shared_paper_db",
        "shared_signal_db_path": "storage/hunter.db",
        "signal_poll_interval_ms": 100,
        "max_entry_signal_age_seconds": 30,
        "source_health_stale_seconds": 150,
        "notify_wecom": True,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        output.chmod(0o600)
    print(
        "live config created "
        f"path={output} source=shared_paper_db leverage=5 "
        f"margin=20% max_positions=3 max_notional={args.max_notional_usdt:.2f}U"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
