from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen

from .config import LiveTradingConfig
from .models import TradeIntent


class LiveEventNotifier:
    def __init__(self, settings: dict[str, Any], config: LiveTradingConfig):
        raw = dict(settings.get("live_trading") or {})
        self.config = config
        self.enabled = bool(raw.get("notify_wecom", True))
        self.notify_dry_run = bool(raw.get("notify_dry_run", False))
        self.webhook_url = (
            (settings.get("notify") or {}).get("wecom_webhook_url")
            or os.environ.get("WECOM_WEBHOOK_URL", "")
        )
        self.last_halt_reason = ""

    def _send(self, content: str) -> tuple[bool, str]:
        if not self.enabled or not self.webhook_url:
            return False, "disabled"
        request = Request(
            self.webhook_url,
            data=json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            return data.get("errcode") == 0, str(data.get("errmsg") or "")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if self.webhook_url:
                message = message.replace(self.webhook_url, "<WECOM_WEBHOOK_URL>")
            return False, message

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def intent_result(self, intent: TradeIntent, result: dict[str, Any]) -> tuple[bool, str]:
        if self.config.mode == "dry_run" and not self.notify_dry_run:
            return False, "dry_run_suppressed"
        order = result.get("order") or {}
        position = result.get("position") or {}
        action = "实盘开空" if intent.action.value == "open_short" else "实盘平空"
        status = str(result.get("status") or order.get("state") or "unknown")
        price = order.get("average_price") or position.get("entry_price") or intent.signal_price
        metadata = dict(position.get("metadata") or {})
        initial_quantity = (
            metadata.get("initial_quantity")
            or position.get("initial_quantity")
            or order.get("filled_quantity")
            or position.get("quantity")
            or "0"
        )
        entry_price = position.get("entry_price") or price or "0"
        leverage = self._number(
            metadata.get("leverage") or self.config.leverage or 1,
            1.0,
        )
        initial_margin = (
            self._number(entry_price) * self._number(initial_quantity) / leverage
            if leverage > 0
            else 0.0
        )
        account = dict(result.get("account") or {})
        equity = account.get("margin_balance") or result.get("account_equity")
        if intent.action.value == "close_short":
            protection = "已平仓" if status == "closed" else "平仓待确认"
        else:
            protection = "已保护" if position.get("protected") else "待确认"
        lines = [
            f"**{action} {intent.symbol}**",
            f"> 状态 {status} | 模式 {self.config.mode}",
            f"> 成交 {price} | 初始保证金 {initial_margin:.4f} USDT",
            f"> 策略 {intent.reason} | {protection}",
        ]
        if equity is not None:
            lines.append(f"> 账户权益 {self._number(equity):.4f} USDT")
        first_fill_time = int(order.get("first_fill_time") or 0)
        if first_fill_time > 0:
            latency_ms = max(0, first_fill_time - int(intent.decision_time))
            lines.append(
                f"> 延迟 {latency_ms}ms | 信号滑点 {order.get('slippage_bps') or '0'}bp"
                f" | 到达滑点 {order.get('arrival_slippage_bps') or '0'}bp"
            )
        if result.get("reason"):
            lines.append(f"> 原因 {result['reason']}")
        return self._send("\n".join(lines))

    def safe_halt(self, reason: str) -> tuple[bool, str]:
        if not reason or reason == self.last_halt_reason:
            return False, "duplicate_or_empty"
        self.last_halt_reason = reason
        return self._send(f"**实盘执行已熔断**\n> SAFE_HALT\n> {reason}")

    def recovered(self, cleared: list[str]) -> tuple[bool, str]:
        if not cleared:
            return False, "empty"
        self.last_halt_reason = ""
        return self._send(
            "**实盘执行已自动恢复**\n"
            "> 权威账户、持仓及订单对账已通过\n"
            f"> 已解除 {', '.join(cleared)}"
        )

    def source_degraded(
        self,
        reason: str,
        detail: dict[str, Any],
    ) -> tuple[bool, str]:
        return self._send(
            "**实盘信号源暂不可用**\n"
            "> 已暂停新开仓，现有仓位仍由交易所止损保护\n"
            f"> 原因 {reason} | 监控延迟 {detail.get('monitor_age_seconds')}s"
        )

    def source_recovered(self, detail: dict[str, Any]) -> tuple[bool, str]:
        return self._send(
            "**实盘信号源已自动恢复**\n"
            "> 纸面与实盘继续共用同一策略信号\n"
            f"> K线延迟 {detail.get('candle_age_seconds')}s | "
            f"监管 {detail.get('universe')} 币"
        )
