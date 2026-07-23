#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


AUTOSTART_CONFIRMATION = "I_UNDERSTAND_BINANCE_LIVE_ORDERS"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Systemd-safe live runner with a fresh one-time authorization nonce.",
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--max-notional-usdt", type=float, required=True)
    args = parser.parse_args()

    if os.environ.get("BINANCE_LIVE_AUTOSTART_CONFIRM") != AUTOSTART_CONFIRMATION:
        raise RuntimeError("BINANCE_LIVE_AUTOSTART_CONFIRM is missing or invalid")

    repo_root = Path(__file__).resolve().parents[1]
    backend = repo_root / "backend"
    os.chdir(backend)
    sys.path.insert(0, str(backend))

    from pump_dump_hunter.config import load_settings
    from pump_dump_hunter.live_trading.config import LiveTradingConfig
    from pump_dump_hunter.live_trading.service import issue_order_nonce

    config_path = Path(args.config).resolve()
    settings = load_settings(config_path)
    config = LiveTradingConfig.from_settings(
        settings,
        mode_override="live",
        max_notional_override=args.max_notional_usdt,
    )
    expected = {
        "signal_source": "shared_paper_db",
        "account_api": "portfolio_margin",
        "position_mode": "hedge",
        "leverage": 5,
        "max_open_positions": 3,
        "sizing_mode": "realized_drawdown_ladder",
    }
    actual = {key: getattr(config, key) for key in expected}
    if actual != expected:
        raise RuntimeError(f"unsafe live server configuration: {actual}")
    if abs(config.base_margin_fraction - 0.20) > 1e-12:
        raise RuntimeError("live base margin fraction must remain 20%")
    if abs(config.margin_fraction_cap - 0.20) > 1e-12:
        raise RuntimeError("live margin fraction cap must remain 20%")
    if not config.sends_real_orders:
        raise RuntimeError("live real-order flags are not enabled")

    nonce = issue_order_nonce(config.ledger_path, ttl_seconds=120)["nonce"]
    env = dict(os.environ)
    env["BINANCE_ORDER_AUTHORIZATION_NONCE"] = str(nonce)
    command = [
        sys.executable,
        "run.py",
        "live-run",
        "--config",
        str(config_path),
        "--mode",
        "live",
        "--max-notional-usdt",
        str(args.max_notional_usdt),
        "--confirm-real-orders",
    ]
    os.execvpe(command[0], command, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
