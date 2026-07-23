from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import resolve_path


VALID_MODES = {"paper", "dry_run", "testnet", "live_micro", "live"}
REAL_ORDER_MODES = {"testnet", "live_micro", "live"}
VALID_EXECUTION_POLICIES = {"market", "ioc", "maker_first", "randomized"}
VALID_ACCOUNT_APIS = {"usdm", "portfolio_margin"}
VALID_POSITION_MODES = {"one_way", "hedge"}
VALID_SIZING_MODES = {"risk_based", "realized_drawdown_ladder"}
VALID_SIGNAL_SOURCES = {"standalone_kline", "shared_paper_db"}


DEFAULT_DRAWDOWN_LADDER = (
    (0.05, 1.0),
    (0.10, 0.75),
    (0.15, 0.50),
    (None, 0.25),
)


@dataclass(frozen=True)
class LiveTradingConfig:
    mode: str
    enabled: bool
    real_order_enabled: bool
    ledger_path: Path
    account_api: str
    position_mode: str
    market_base_url: str
    rest_base_url: str
    ws_trade_url: str
    ws_stream_url: str
    recv_window_ms: int
    leverage: int
    isolated_margin: bool
    require_one_way: bool
    max_open_positions: int
    sizing_mode: str
    base_margin_fraction: float
    drawdown_ladder: tuple[tuple[float | None, float], ...]
    risk_per_trade: float
    margin_fraction_cap: float
    max_notional_usdt: float
    liquidation_stop_buffer_pct: float
    max_entry_slippage_bps: float
    ioc_slippage_bps: float
    maker_wait_ms: int
    maker_wait_candidates_ms: tuple[int, ...]
    execution_policy: str
    protection_timeout_ms: int
    reconcile_interval_seconds: int
    private_stream_stale_seconds: int
    daily_loss_limit_pct: float
    allowed_symbols: tuple[str, ...]
    dashboard_enabled: bool
    signal_source: str
    shared_signal_db_path: Path
    signal_poll_interval_ms: int
    max_entry_signal_age_seconds: int
    source_health_stale_seconds: int

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any],
        mode_override: str | None = None,
        max_notional_override: float | None = None,
    ) -> "LiveTradingConfig":
        raw = dict(settings.get("live_trading") or {})
        mode = str(mode_override or raw.get("mode") or "dry_run").lower()
        if mode not in VALID_MODES:
            raise ValueError(f"unknown live trading mode: {mode}")
        policy = str(raw.get("execution_policy") or "randomized").lower()
        if policy not in VALID_EXECUTION_POLICIES:
            raise ValueError(f"unknown execution policy: {policy}")
        account_api = str(raw.get("account_api") or "portfolio_margin").lower()
        if account_api not in VALID_ACCOUNT_APIS:
            raise ValueError(f"unknown live account API: {account_api}")
        position_mode = str(raw.get("position_mode") or "hedge").lower()
        if position_mode not in VALID_POSITION_MODES:
            raise ValueError(f"unknown live position mode: {position_mode}")
        sizing_mode = str(raw.get("sizing_mode") or "risk_based").lower()
        if sizing_mode not in VALID_SIZING_MODES:
            raise ValueError(f"unknown live sizing mode: {sizing_mode}")
        ladder_rows = raw.get("drawdown_ladder")
        if ladder_rows is None:
            drawdown_ladder = DEFAULT_DRAWDOWN_LADDER
        else:
            drawdown_ladder = tuple(
                (
                    None if row.get("below") is None else float(row["below"]),
                    float(row["factor"]),
                )
                for row in ladder_rows
            )
        ledger = Path(raw.get("ledger_path") or "storage/live_trading.db")
        if not ledger.is_absolute():
            ledger = resolve_path(ledger)
        shared_signal_db = Path(
            raw.get("shared_signal_db_path")
            or (settings.get("paths") or {}).get("db_path")
            or "storage/hunter.db"
        )
        if not shared_signal_db.is_absolute():
            shared_signal_db = resolve_path(shared_signal_db)
        signal_source = str(raw.get("signal_source") or "standalone_kline").lower()
        testnet = mode == "testnet"
        if testnet and account_api == "portfolio_margin":
            raise ValueError("Binance Portfolio Margin is not supported by the USD-M testnet")
        market_base = str(
            raw.get("testnet_market_base_url" if testnet else "market_base_url")
            or ("https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com")
        ).rstrip("/")
        rest_base = str(
            raw.get("testnet_rest_base_url" if testnet else "rest_base_url")
            or (
                "https://testnet.binancefuture.com"
                if testnet
                else "https://papi.binance.com" if account_api == "portfolio_margin"
                else "https://fapi.binance.com"
            )
        ).rstrip("/")
        ws_trade = str(
            raw.get("testnet_ws_trade_url" if testnet else "ws_trade_url")
            or (
                "wss://testnet.binancefuture.com/ws-fapi/v1"
                if testnet
                else "" if account_api == "portfolio_margin"
                else "wss://ws-fapi.binance.com/ws-fapi/v1"
            )
        )
        ws_stream = str(
            raw.get("testnet_ws_stream_url" if testnet else "ws_stream_url")
            or (
                "wss://fstream.binancefuture.com"
                if testnet
                else "wss://fstream.binance.com/pm" if account_api == "portfolio_margin"
                else "wss://fstream.binance.com"
            )
        ).rstrip("/")
        max_notional = float(
            max_notional_override
            if max_notional_override is not None
            else raw.get("max_notional_usdt", 20.0)
        )
        cfg = cls(
            mode=mode,
            enabled=bool(raw.get("enabled", False)),
            real_order_enabled=bool(raw.get("real_order_enabled", False)),
            ledger_path=ledger,
            account_api=account_api,
            position_mode=position_mode,
            market_base_url=market_base,
            rest_base_url=rest_base,
            ws_trade_url=ws_trade,
            ws_stream_url=ws_stream,
            recv_window_ms=int(raw.get("recv_window_ms", 2000)),
            leverage=int(raw.get("leverage", 3)),
            isolated_margin=bool(raw.get("isolated_margin", account_api != "portfolio_margin")),
            require_one_way=position_mode == "one_way",
            max_open_positions=int(raw.get("max_open_positions", 1)),
            sizing_mode=sizing_mode,
            base_margin_fraction=float(
                raw.get("base_margin_fraction", raw.get("margin_fraction_cap", 0.05))
            ),
            drawdown_ladder=drawdown_ladder,
            risk_per_trade=float(raw.get("risk_per_trade", 0.0025)),
            margin_fraction_cap=float(raw.get("margin_fraction_cap", 0.05)),
            max_notional_usdt=max_notional,
            liquidation_stop_buffer_pct=float(raw.get("liquidation_stop_buffer_pct", 0.05)),
            max_entry_slippage_bps=float(raw.get("max_entry_slippage_bps", 30.0)),
            ioc_slippage_bps=float(raw.get("ioc_slippage_bps", 12.0)),
            maker_wait_ms=int(raw.get("maker_wait_ms", 250)),
            maker_wait_candidates_ms=tuple(int(x) for x in raw.get("maker_wait_candidates_ms", [150, 250, 350, 500])),
            execution_policy=policy,
            protection_timeout_ms=int(raw.get("protection_timeout_ms", 1000)),
            reconcile_interval_seconds=int(raw.get("reconcile_interval_seconds", 30)),
            private_stream_stale_seconds=int(raw.get("private_stream_stale_seconds", 10)),
            daily_loss_limit_pct=float(raw.get("daily_loss_limit_pct", 0.02)),
            allowed_symbols=tuple(str(x).upper() for x in raw.get("allowed_symbols", [])),
            dashboard_enabled=bool(raw.get("dashboard_enabled", False)),
            signal_source=signal_source,
            shared_signal_db_path=shared_signal_db,
            signal_poll_interval_ms=int(raw.get("signal_poll_interval_ms", 100)),
            max_entry_signal_age_seconds=int(raw.get("max_entry_signal_age_seconds", 30)),
            source_health_stale_seconds=int(raw.get("source_health_stale_seconds", 150)),
        )
        cfg.validate()
        return cfg

    @property
    def sends_real_orders(self) -> bool:
        return self.enabled and self.real_order_enabled and self.mode in REAL_ORDER_MODES

    @property
    def position_side(self) -> str:
        return "SHORT" if self.position_mode == "hedge" else "BOTH"

    @property
    def exchange_reduce_only(self) -> bool:
        return self.position_mode == "one_way"

    def validate(self) -> None:
        if self.recv_window_ms <= 0 or self.recv_window_ms > 60_000:
            raise ValueError("recv_window_ms must be in 1..60000")
        if self.leverage < 1 or self.leverage > 20:
            raise ValueError("live leverage must be in 1..20")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be positive")
        if self.sizing_mode not in VALID_SIZING_MODES:
            raise ValueError(f"unknown live sizing mode: {self.sizing_mode}")
        if self.signal_source not in VALID_SIGNAL_SOURCES:
            raise ValueError(f"unknown live signal source: {self.signal_source}")
        if self.shared_signal_db_path.resolve() == self.ledger_path.resolve():
            raise ValueError("shared signal DB and live ledger must be separate files")
        if self.signal_poll_interval_ms < 50 or self.signal_poll_interval_ms > 5_000:
            raise ValueError("signal_poll_interval_ms must be in 50..5000")
        if self.max_entry_signal_age_seconds < 1 or self.max_entry_signal_age_seconds > 300:
            raise ValueError("max_entry_signal_age_seconds must be in 1..300")
        if self.source_health_stale_seconds < 60 or self.source_health_stale_seconds > 600:
            raise ValueError("source_health_stale_seconds must be in 60..600")
        if not 0 < self.base_margin_fraction <= 0.25:
            raise ValueError("base_margin_fraction must be in (0, 0.25]")
        prior = -1.0
        if not self.drawdown_ladder or self.drawdown_ladder[-1][0] is not None:
            raise ValueError("drawdown_ladder must end with below=null")
        for index, (below, factor) in enumerate(self.drawdown_ladder):
            if not 0 <= factor <= 1:
                raise ValueError("drawdown_ladder factors must be between 0 and 1")
            if below is None:
                if index != len(self.drawdown_ladder) - 1:
                    raise ValueError("only the final drawdown threshold may be null")
                continue
            if below <= prior or below <= 0:
                raise ValueError("drawdown thresholds must be positive and increasing")
            prior = below
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("risk_per_trade must be in (0, 0.02]")
        if not 0 < self.margin_fraction_cap <= 0.25:
            raise ValueError("margin_fraction_cap must be in (0, 0.25]")
        if (
            self.sizing_mode == "realized_drawdown_ladder"
            and self.margin_fraction_cap < self.base_margin_fraction
        ):
            raise ValueError(
                "margin_fraction_cap must not be below base_margin_fraction in drawdown-ladder mode"
            )
        if self.max_notional_usdt <= 0:
            raise ValueError("max_notional_usdt must be positive")
        if self.max_entry_slippage_bps <= 0 or self.max_entry_slippage_bps > 500:
            raise ValueError("max_entry_slippage_bps must be in (0, 500]")
        if self.ioc_slippage_bps <= 0 or self.ioc_slippage_bps > self.max_entry_slippage_bps:
            raise ValueError("ioc_slippage_bps must be positive and no larger than max_entry_slippage_bps")
        if self.protection_timeout_ms <= 0 or self.protection_timeout_ms > 10_000:
            raise ValueError("protection_timeout_ms must be in 1..10000")
        if self.reconcile_interval_seconds <= 0 or self.reconcile_interval_seconds > 300:
            raise ValueError("reconcile_interval_seconds must be in 1..300")
        if not 0 < self.daily_loss_limit_pct <= 0.50:
            raise ValueError("daily_loss_limit_pct must be in (0, 0.50]")
        if self.mode in {"paper", "dry_run"} and self.real_order_enabled:
            raise ValueError(f"real_order_enabled cannot be true in {self.mode} mode")
        if self.mode == "live" and self.max_open_positions > 3:
            raise ValueError("initial live mode is capped at three concurrent positions")
        if self.account_api == "portfolio_margin" and self.isolated_margin:
            raise ValueError("Portfolio Margin does not support per-symbol isolated margin")

    def drawdown_factor(self, drawdown_pct: float) -> float:
        for below, factor in self.drawdown_ladder:
            if below is None or drawdown_pct < below:
                return factor
        return 1.0
