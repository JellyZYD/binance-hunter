from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from ..models import Alert
from ..timeutils import iso_from_ms, local_day_from_ms


class AlertSink:
    def __init__(self, alerts_dir: str | Path, webhook_url: str | None = None):
        self.alerts_dir = Path(alerts_dir)
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("WECOM_WEBHOOK_URL", "")

    def emit(self, alert: Alert) -> tuple[bool, str]:
        print(render_console_alert(alert), flush=True)
        self.write_files(alert)
        if self.webhook_url:
            return self.push_wecom(alert)
        return False, ""

    def write_files(self, alert: Alert) -> None:
        day = local_day_from_ms(alert.decision_time)
        jsonl = self.alerts_dir / f"{day}.jsonl"
        md = self.alerts_dir / f"{day}.md"
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")
        with md.open("a", encoding="utf-8") as fh:
            fh.write(render_markdown_alert(alert) + "\n\n")

    def push_wecom(self, alert: Alert) -> tuple[bool, str]:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": render_wecom_markdown(alert)},
        }
        req = Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return True, body
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]


def render_console_alert(alert: Alert) -> str:
    return (
        f"[{iso_from_ms(alert.decision_time)}] {alert.level} {alert.symbol} "
        f"price={alert.price} invalid={alert.invalidation_price} "
        f"remaining={alert.remaining_downside_pct:.2f}% vol={alert.volume_ratio:.2f}x"
    )


def render_markdown_alert(alert: Alert) -> str:
    evidence = "; ".join(alert.evidence)
    risks = "; ".join(alert.risks) or "-"
    cat = f" [{alert.category}]" if alert.category else ""
    seq = f" 第{alert.occurrence}次" if alert.occurrence else ""
    return "\n".join(
        [
            f"### {alert.level}{seq} {alert.symbol}{cat} {iso_from_ms(alert.decision_time)}",
            f"- price: {alert.price}",
            f"- invalidation: {alert.invalidation_price}",
            f"- high/anchor: {alert.high_price} / {alert.anchor_price}",
            f"- remaining_to_anchor: {alert.remaining_downside_pct:.2f}%",
            f"- volume_ratio: {alert.volume_ratio:.2f}x",
            f"- evidence: {evidence}",
            f"- risks: {risks}",
        ]
    )


LEVEL_CN = {"early_alert": "顶部预警", "short_signal": "下跌启动", "fallback_alert": "回落兜底", "long_signal": "做多"}


def render_wecom_markdown(alert: Alert) -> str:
    name = LEVEL_CN.get(alert.level, alert.level)
    tier = next((e.split("=", 1)[1] for e in alert.evidence if e.startswith("置信=")), "")
    score = next((e.split("=", 1)[1] for e in alert.evidence if e.startswith("ML") and "分=" in e), "")
    hint = next((e.replace("经验", "") for e in alert.evidence if e.startswith("经验见底")), "")
    tags = []
    if alert.category and alert.category != "做多":
        tags.append(alert.category)
    if tier and tier != "普通":  # 只在高置信时标出, 普通档为默认不标
        tags.append(tier)
    tag = f" [{'·'.join(tags)}]" if tags else ""
    url = f"https://www.binance.com/zh-CN/futures/{alert.symbol}"
    if alert.level == "long_signal":
        from_entry = next((e.split("=", 1)[1] for e in alert.evidence if e.startswith("距入场=")), "")
        metrics = f"现价 {alert.price}" + (f" · 距入场 {from_entry}" if from_entry else "") + f" · 止损 {alert.invalidation_price}"
    else:
        metrics = f"现价 {alert.price} · 距锚点 {alert.remaining_downside_pct:.1f}% · 量比 {alert.volume_ratio:.1f}x"
        if hint:
            metrics += f" · {hint}"
    if score:
        metrics += f" · ML分{score}"
    seq = f" 第{alert.occurrence}次" if alert.occurrence else ""
    return "\n".join([
        f"**{name}{seq} · {alert.symbol}{tag}**",
        f"> {metrics}",
        f"币安合约: {url}",
    ])


def export_day(alerts_dir: str | Path, day: str) -> Path:
    alerts_dir = Path(alerts_dir)
    md = alerts_dir / f"{day}.md"
    if not md.exists():
        md.write_text(f"# Alerts {day}\n\nNo alerts.\n", encoding="utf-8")
    return md
