from __future__ import annotations

import json
import os
from pathlib import Path
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
        if self.webhook_url and should_push_wecom(alert):
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
        payload = {"msgtype": "markdown", "markdown": {"content": render_wecom_markdown(alert)}}
        req = Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return False, f"invalid wecom response: {body[:160]}"
            errcode = data.get("errcode")
            if errcode not in (0, "0"):
                errmsg = data.get("errmsg", body)
                return False, f"wecom errcode={errcode} errmsg={errmsg}"[:200]
            return True, body
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]


PUSH_LEVELS = {"long_signal", "early_alert", "short_signal"}

LEVEL_CN = {
    "long_signal": "做多",
    "early_alert": "见顶",
    "short_signal": "做空",
    "distribution_warning": "派发预警",
    "long_timeout": "做多超时",
    "fallback_alert": "回落兜底",
}

MODE_CN = {
    "fast_dump": "快拉急跌",
    "slow_distribution": "高位派发",
    "long_entry": "做多启动",
    "trend_watch": "趋势观察",
    "risk_watch": "风险观察",
    "unknown_watch": "等待分型",
    "continuation_watch": "继续上涨/禁空",
    "second_distribution_watch": "二次高位观察",
    "distribution_warning": "派发预警",
    "completed": "已结束",
    "router_missing": "路由缺失",
}

STATE_CN = {
    "acceleration": "加速",
    "trend_hold": "趋势保持",
    "distribution": "派发",
    "climax_risk": "冲顶风险",
    "pullback_risk": "回落风险",
    "breakdown": "破位",
    "entry_watch": "入场观察",
    "neutral_watch": "中性观察",
}


def should_push_wecom(alert: Alert) -> bool:
    return alert.level in PUSH_LEVELS


def render_console_alert(alert: Alert) -> str:
    meta = render_lifecycle_inline(alert)
    return (
        f"[{iso_from_ms(alert.decision_time)}] {alert.level} {alert.symbol} "
        f"price={alert.price} invalid={alert.invalidation_price} "
        f"remaining={alert.remaining_downside_pct:.2f}% vol={alert.volume_ratio:.2f}x{meta}"
    )


def render_lifecycle_inline(alert: Alert) -> str:
    parts = []
    if alert.signal_interval:
        parts.append(f"interval={alert.signal_interval}")
    if alert.lifecycle_mode:
        parts.append(f"mode={alert.lifecycle_mode}")
    if alert.behavior_state:
        parts.append(f"state={alert.behavior_state}")
    if alert.model_name:
        parts.append(f"model={alert.model_name}")
    if alert.model_score:
        parts.append(f"score={alert.model_score:.3f}")
    if alert.model_threshold:
        parts.append(f"thr={alert.model_threshold:.3f}")
    if alert.route_mode:
        parts.append(f"route={alert.route_mode}")
    if alert.route_confidence:
        parts.append(f"route_conf={alert.route_confidence:.3f}")
    return " " + " ".join(parts) if parts else ""


def render_markdown_alert(alert: Alert) -> str:
    evidence = "; ".join(alert.evidence)
    risks = "; ".join(alert.risks) or "-"
    cat = f" [{alert.category}]" if alert.category else ""
    lifecycle = render_lifecycle_inline(alert).strip() or "-"
    seq = f" 第{alert.occurrence}次" if alert.occurrence else ""
    return "\n".join(
        [
            f"### {LEVEL_CN.get(alert.level, alert.level)}{seq} {alert.symbol}{cat} {iso_from_ms(alert.decision_time)}",
            f"- price: {alert.price}",
            f"- invalidation: {alert.invalidation_price}",
            f"- high/anchor: {alert.high_price} / {alert.anchor_price}",
            f"- remaining_to_anchor: {alert.remaining_downside_pct:.2f}%",
            f"- volume_ratio: {alert.volume_ratio:.2f}x",
            f"- lifecycle: {lifecycle}",
            f"- evidence: {evidence}",
            f"- risks: {risks}",
        ]
    )


def render_wecom_markdown(alert: Alert) -> str:
    name = LEVEL_CN.get(alert.level, alert.level)
    seq = f" 第{alert.occurrence}次" if alert.occurrence else ""
    confidence = confidence_text(alert)
    mode = MODE_CN.get(alert.lifecycle_mode, alert.lifecycle_mode or alert.category or "-")
    state = STATE_CN.get(alert.behavior_state, alert.behavior_state or "-")
    interval = f"{alert.signal_interval} " if alert.signal_interval else ""
    model = f" / {alert.model_name}" if alert.model_name else ""
    route = route_text(alert)
    return "\n".join(
        [
            f"**{name}{seq} {alert.symbol}**",
            f"> 价格 {fmt_price(alert.price)} | 失效 {fmt_price(alert.invalidation_price)}",
            f"> 置信 {confidence}",
            f"> 状态 {interval}{state} | 类型 {mode}{model}{route}",
        ]
    )


def confidence_text(alert: Alert) -> str:
    tier = evidence_value(alert.evidence, "tier") or evidence_value(alert.evidence, "置信")
    tier = {"high": "高置信", "normal": "普通"}.get(tier, tier)
    if alert.model_score:
        score = f"{alert.model_score:.3f}"
        if alert.model_threshold:
            score += f"/{alert.model_threshold:.3f}"
        return f"{tier} {score}".strip()
    score = evidence_value(alert.evidence, "score")
    threshold = evidence_value(alert.evidence, "threshold")
    if score:
        return f"{tier} {score}/{threshold}".strip() if threshold else f"{tier} {score}".strip()
    ml_score = next((e.split("=", 1)[1] for e in alert.evidence if e.startswith("ML") and "=" in e), "")
    return f"{tier} {ml_score}".strip() or "-"


def route_text(alert: Alert) -> str:
    if not alert.route_mode:
        return ""
    value = alert.route_mode
    if alert.route_confidence:
        value += f" {alert.route_confidence:.3f}"
    if alert.route_margin:
        value += f"/m{alert.route_margin:.3f}"
    return f" | route {value}"


def evidence_value(evidence: list[str], key: str) -> str:
    prefix = f"{key}="
    return next((e.split("=", 1)[1] for e in evidence if e.startswith(prefix)), "")


def fmt_price(value: float) -> str:
    return f"{value:.8g}"


def export_day(alerts_dir: str | Path, day: str) -> Path:
    alerts_dir = Path(alerts_dir)
    md = alerts_dir / f"{day}.md"
    if not md.exists():
        md.write_text(f"# Alerts {day}\n\nNo alerts.\n", encoding="utf-8")
    return md
