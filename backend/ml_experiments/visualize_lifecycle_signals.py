"""Build a local HTML candlestick visualizer for lifecycle replay signals.

The visualizer reuses the production lifecycle router/expert model files and
the same replay helpers used by the current strategy backtest. It is intended
for manual inspection: pick a lifecycle, inspect candles, route probabilities,
and every signal emitted by the current production strategy.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import sqlite3
import sys
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from ml_experiments.backtest_lifecycle_router_replay import (  # noqa: E402
    add_slow_features_vectorized,
    combine_signal_streams,
    dynamic_thresholds,
    prepare_dense,
    replay_high_pump_strategy,
    replay_router_strategy,
    score_all,
    split_times,
)
from pump_dump_hunter.ml import lifecycle as life  # noqa: E402
from pump_dump_hunter.ml.model import MLScorer  # noqa: E402


MS_15M = 15 * 60_000
DEFAULT_SETTINGS = ROOT / "backend" / "config" / "settings.json"


@dataclass
class StrategyReplayConfig:
    confirm_bars: int
    cooldown_bars: int
    margin: float
    dynamic_thresholds: bool
    fast_trend_threshold: float
    slow_trend_threshold: float
    fast_break_threshold: float
    slow_break_threshold: float
    slow_mature_threshold: float
    pump_signal_min_gain_pct: float
    high_pump_enabled: bool
    high_pump_dense: str


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dense_path = resolve_path(args.dense)
    models_dir = resolve_path(args.models_dir)
    out_path = resolve_path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    settings = read_json(resolve_path(args.settings))
    replay_cfg = strategy_replay_config(settings)
    replay_cfg.high_pump_dense = args.high_pump_dense
    replay_args = argparse.Namespace(
        dynamic_thresholds=replay_cfg.dynamic_thresholds,
        fast_trend_threshold=replay_cfg.fast_trend_threshold,
        slow_trend_threshold=replay_cfg.slow_trend_threshold,
        fast_break_threshold=replay_cfg.fast_break_threshold,
        slow_break_threshold=replay_cfg.slow_break_threshold,
        slow_mature_threshold=replay_cfg.slow_mature_threshold,
    )

    dense = prepare_dense(pd.read_parquet(dense_path).copy())
    split = split_times(dense)
    rows = dense[dense["entry_time"] >= split["test_start"]].copy()
    if args.all_rows:
        rows = dense.copy()

    scorer = MLScorer(models_dir)
    if not scorer.lifecycle_ready or not scorer.lifecycle_router_ready:
        raise RuntimeError(f"lifecycle models not ready: {scorer.error}")

    scores = score_all(rows, scorer)
    router_signals = replay_router_strategy(
        rows,
        scores,
        scorer,
        replay_cfg.confirm_bars,
        replay_cfg.cooldown_bars,
        replay_cfg.margin,
        replay_args,
        pump_signal_min_gain_pct=replay_cfg.pump_signal_min_gain_pct,
    )
    high_args = argparse.Namespace(
        cooldown_bars=replay_cfg.cooldown_bars,
        high_pump_dense=replay_cfg.high_pump_dense,
    )
    high_signals = replay_high_pump_strategy(dense, rows, scorer, high_args) if replay_cfg.high_pump_enabled else pd.DataFrame()
    signals = combine_signal_streams(router_signals, high_signals, replay_cfg.cooldown_bars)
    timeline = build_route_timeline(rows, scores, scorer, replay_cfg, replay_args)
    selected_ids = select_lifecycles(rows, signals, args)
    if not selected_ids:
        raise RuntimeError("no lifecycles selected; relax filters or pass --include-no-signal")

    candles_db = resolve_path(args.candles_db)
    charts = [
        build_chart_payload(life_id, rows, signals, timeline, candles_db, args)
        for life_id in selected_ids[: max(1, args.limit)]
    ]

    payload = {
        "generated_at": iso_ms(now_ms()),
        "strategy": {
            "version": settings.get("signals", {}).get("strategy_version", ""),
            "long_interval": settings.get("signals", {}).get("long_interval", ""),
            "expert_interval": settings.get("signals", {}).get("confirm_interval", ""),
            "router": (scorer.lifecycle_meta or {}).get("router", ""),
            "route": (scorer.lifecycle_meta or {}).get("route", {}),
            "replay": asdict(replay_cfg),
            "signal_streams": {
                "router": int(len(router_signals)),
                "high_pump": int(len(high_signals)),
                "combined": int(len(signals)),
            },
            "split": split,
            "row_scope": "all" if args.all_rows else "holdout_test",
        },
        "charts": charts,
    }
    out_path.write_text(render_html(payload), encoding="utf-8")
    result = {"html": str(out_path), "charts": len(charts)}
    if args.serve:
        serve_html(out_path, args.host, args.port, open_browser=not args.no_open)
    elif args.open:
        webbrowser.open(out_path.as_uri())
        result["opened"] = True
        print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Visualize lifecycle replay signals on local candlestick charts.")
    parser.add_argument("--dense", default="backend/storage/ml/dense_lifecycle/dense_15m.parquet")
    parser.add_argument("--models-dir", default="backend/pump_dump_hunter/ml/models")
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
    parser.add_argument("--candles-db", default="backend/storage/hunter_bb_300_v2.db")
    parser.add_argument("--out", default="backend/storage/ml/lifecycle_visualizations/lifecycle_signals.html")
    parser.add_argument("--symbol", default="", help="Optional symbol filter, e.g. TLMUSDT.")
    parser.add_argument("--life-id", default="", help="Exact life_id from the dense replay dataset.")
    parser.add_argument("--family", default="", choices=["", "fast_dump", "slow_distribution", "second_distribution"])
    parser.add_argument("--level", default="", choices=["", "short_signal", "early_alert", "distribution_warning"])
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--pre-hours", type=float, default=12.0)
    parser.add_argument("--post-hours", type=float, default=72.0)
    parser.add_argument("--include-no-signal", action="store_true")
    parser.add_argument("--hide-internal-warnings", action="store_true")
    parser.add_argument("--all-rows", action="store_true", help="Use all dense rows instead of the holdout test split.")
    parser.add_argument("--high-pump-dense", default="backend/storage/ml/high_pump40_experts/high_pump_40_dense.parquet")
    parser.add_argument("--open", action="store_true", help="Open the generated HTML in the default browser and exit.")
    parser.add_argument("--serve", action="store_true", help="Serve the generated HTML through a local HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--no-open", action="store_true", help="With --serve, do not open the browser automatically.")
    return parser.parse_args(argv)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def strategy_replay_config(settings: dict[str, Any]) -> StrategyReplayConfig:
    signals = settings.get("signals", {})
    cooldown_hours = float(signals.get("multi_signal_cooldown_hours", 2.0) or 2.0)
    return StrategyReplayConfig(
        confirm_bars=int(signals.get("lifecycle_route_confirm_bars", 2) or 2),
        cooldown_bars=max(1, int(round(cooldown_hours * 60 / 15))),
        margin=float(signals.get("lifecycle_route_margin", life.DEFAULT_ROUTE_MARGIN) or life.DEFAULT_ROUTE_MARGIN),
        dynamic_thresholds=bool(signals.get("lifecycle_dynamic_route_thresholds", True)),
        fast_trend_threshold=float(signals.get("lifecycle_route_fast_trend_threshold", 0.97) or 0.97),
        slow_trend_threshold=float(signals.get("lifecycle_route_slow_trend_threshold", 0.82) or 0.82),
        fast_break_threshold=float(
            signals.get("lifecycle_route_fast_break_threshold", life.DEFAULT_ROUTE_THRESHOLDS["fast_dump"])
            or life.DEFAULT_ROUTE_THRESHOLDS["fast_dump"]
        ),
        slow_break_threshold=float(
            signals.get("lifecycle_route_slow_break_threshold", life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])
            or life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"]
        ),
        slow_mature_threshold=float(
            signals.get("lifecycle_route_slow_mature_threshold", life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])
            or life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"]
        ),
        pump_signal_min_gain_pct=float(signals.get("lifecycle_pump_signal_min_gain_pct", 0.0) or 0.0),
        high_pump_enabled=bool(signals.get("lifecycle_high_pump_enabled", False)),
        high_pump_dense="backend/storage/ml/high_pump40_experts/high_pump_40_dense.parquet",
    )


def build_route_timeline(
    rows: pd.DataFrame,
    scores: dict[str, Any],
    scorer: MLScorer,
    cfg: StrategyReplayConfig,
    replay_args: argparse.Namespace,
) -> pd.DataFrame:
    base_thresholds = dict(life.DEFAULT_ROUTE_THRESHOLDS)
    base_thresholds.update(((scorer.lifecycle_meta or {}).get("route") or {}).get("thresholds", {}))
    probs_df: pd.DataFrame = scores["family_router"]
    out: list[dict[str, Any]] = []
    for life_id, group in rows.sort_values(["life_id", "decision_time"]).groupby("life_id", sort=False):
        candidate = "unknown"
        streak = 0
        for idx, row in group.iterrows():
            probs = probs_df.loc[idx].dropna().to_dict()
            if probs:
                route = life.route_from_probabilities(
                    probs,
                    thresholds=dynamic_thresholds(row, base_thresholds, replay_args),
                    margin_threshold=cfg.margin,
                )
            else:
                route = {"mode": "unknown", "candidate": "unknown", "confidence": 0.0, "margin": 0.0, "probs": {}}
            raw_mode = str(route["mode"] or "unknown")
            if raw_mode == "unknown":
                candidate = "unknown"
                streak = 0
                confirmed = "unknown"
            else:
                streak = streak + 1 if candidate == raw_mode else 1
                candidate = raw_mode
                confirmed = raw_mode if streak >= max(1, cfg.confirm_bars) else "unknown"
            probs_out = {k: float(route.get("probs", {}).get(k, 0.0) or 0.0) for k in life.FAMILY_ORDER}
            out.append(
                {
                    "life_id": str(life_id),
                    "symbol": str(row["symbol"]),
                    "decision_time": int(row["decision_time"]),
                    "price": float(row["current_price"]),
                    "behavior_state": str(row.get("behavior_state", "")),
                    "route_mode": confirmed,
                    "route_candidate": str(route.get("candidate", "unknown")),
                    "route_confidence": float(route.get("confidence", 0.0) or 0.0),
                    "route_margin": float(route.get("margin", 0.0) or 0.0),
                    "route_streak": int(streak),
                    **{f"p_{name}": value for name, value in probs_out.items()},
                }
            )
    return pd.DataFrame(out)


def select_lifecycles(rows: pd.DataFrame, signals: pd.DataFrame, args: argparse.Namespace) -> list[str]:
    frame = rows
    if args.symbol:
        frame = frame[frame["symbol"].astype(str).str.upper() == args.symbol.upper()]
    if args.life_id:
        frame = frame[frame["life_id"].astype(str) == args.life_id]
    if args.family:
        frame = frame[frame["family"].astype(str) == args.family]
    allowed = set(frame["life_id"].astype(str).unique())
    if not allowed:
        return []
    sig = signals[signals["life_id"].astype(str).isin(allowed)].copy()
    if args.level:
        sig = sig[sig["level"] == args.level]
    if args.hide_internal_warnings:
        sig = sig[sig["level"] != "distribution_warning"]
    if len(sig):
        score = (
            sig.assign(priority=sig["level"].map({"short_signal": 3, "early_alert": 2, "distribution_warning": 1}).fillna(0))
            .groupby("life_id")
            .agg(signal_count=("level", "size"), priority=("priority", "max"), first_time=("decision_time", "min"))
            .sort_values(["priority", "signal_count", "first_time"], ascending=[False, False, True])
        )
        ids = [str(x) for x in score.index.tolist()]
        if args.include_no_signal:
            ids.extend([x for x in allowed if x not in ids])
        return ids
    return sorted(allowed) if args.include_no_signal else []


def build_chart_payload(
    life_id: str,
    rows: pd.DataFrame,
    signals: pd.DataFrame,
    timeline: pd.DataFrame,
    candles_db: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    group = rows[rows["life_id"].astype(str) == str(life_id)].sort_values("decision_time").copy()
    if group.empty:
        raise ValueError(f"life_id not found: {life_id}")
    first = group.iloc[0]
    symbol = str(first["symbol"])
    entry_time = int(first["entry_time"])
    end_time = int(group["decision_time"].max() + args.post_hours * 3_600_000)
    start_time = int(entry_time - args.pre_hours * 3_600_000)
    candles, candle_source = load_15m_candles(candles_db, symbol, start_time, end_time)
    if not candles:
        candles, candle_source = synthetic_candles(group), "synthetic_dense_close"
    sig = signals[signals["life_id"].astype(str) == str(life_id)].sort_values("decision_time").copy()
    if args.hide_internal_warnings:
        sig = sig[sig["level"] != "distribution_warning"]
    route = timeline[timeline["life_id"].astype(str) == str(life_id)].sort_values("decision_time").copy()
    entry_marker = {
        "time": iso_ms(entry_time),
        "ts": entry_time,
        "price": float(first["entry_price"]),
        "level": "lifecycle_entry",
        "label": "生命周期启动/入池",
        "model": "",
        "score": None,
        "threshold": None,
        "route_mode": "",
        "behavior_state": "",
        "hover": f"{symbol}<br>生命周期启动/入池<br>entry={float(first['entry_price']):g}<br>{iso_ms(entry_time)}",
    }
    signal_markers = [entry_marker] + [signal_payload(row) for _, row in sig.iterrows()]
    high_since_entry = float(group["ctx_high_since_entry"].max() or 0.0)
    family_counts = group["family"].value_counts().to_dict()
    return {
        "life_id": str(life_id),
        "symbol": symbol,
        "family": str(first.get("family", "")),
        "entry_time": iso_ms(entry_time),
        "entry_price": float(first["entry_price"]),
        "max_gain_pct": round(high_since_entry * 100, 2),
        "rows": int(len(group)),
        "candles_source": candle_source,
        "family_counts": {str(k): int(v) for k, v in family_counts.items()},
        "candles": candles,
        "signals": signal_markers,
        "routes": route_payload(route),
        "summary": signal_summary(sig),
    }


def signal_payload(row: pd.Series) -> dict[str, Any]:
    level = str(row["level"])
    model = str(row.get("model", ""))
    score = float(row["score"]) if pd.notna(row.get("score")) else None
    threshold = float(row["threshold"]) if pd.notna(row.get("threshold")) else None
    price = float(row.get("current_price", np.nan))
    hover = [
        f"{row['symbol']} {signal_label(level)}",
        f"model={model}",
        f"price={price:g}",
        f"score={score:.3f}" if score is not None else "",
        f"threshold={threshold:.3f}" if threshold is not None else "",
        f"route={row.get('route_mode', '')}",
        f"state={row.get('behavior_state', '')}",
        f"future_drop_6h={pct(row.get('future_drop_6h'))}",
        f"future_drop_24h={pct(row.get('future_drop_24h'))}",
        f"adverse_24h={pct(row.get('short_adverse_before_down5_24h'))}",
        iso_ms(int(row["decision_time"])),
    ]
    return {
        "time": iso_ms(int(row["decision_time"])),
        "ts": int(row["decision_time"]),
        "price": price,
        "level": level,
        "label": signal_label(level),
        "model": model,
        "score": score,
        "threshold": threshold,
        "route_mode": str(row.get("route_mode", "")),
        "behavior_state": str(row.get("behavior_state", "")),
        "future_drop_6h": none_or_float(row.get("future_drop_6h")),
        "future_drop_24h": none_or_float(row.get("future_drop_24h")),
        "future_up_24h": none_or_float(row.get("future_up_24h")),
        "short_adverse_24h": none_or_float(row.get("short_adverse_before_down5_24h")),
        "hover": "<br>".join(x for x in hover if x),
    }


def route_payload(route: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _, row in route.iterrows():
        out.append(
            {
                "time": iso_ms(int(row["decision_time"])),
                "ts": int(row["decision_time"]),
                "price": float(row["price"]),
                "route_mode": str(row["route_mode"]),
                "route_candidate": str(row["route_candidate"]),
                "behavior_state": str(row["behavior_state"]),
                "route_confidence": float(row["route_confidence"]),
                "route_margin": float(row["route_margin"]),
                "route_streak": int(row["route_streak"]),
                "p_fast": float(row.get("p_fast_dump", 0.0)),
                "p_slow": float(row.get("p_slow_distribution", 0.0)),
                "p_second": float(row.get("p_second_distribution", 0.0)),
                "p_continuation": float(row.get("p_continuation", 0.0)),
            }
        )
    return out


def signal_summary(sig: pd.DataFrame) -> dict[str, Any]:
    if sig.empty:
        return {"signals": 0}
    return {
        "signals": int(len(sig)),
        "by_level": {str(k): int(v) for k, v in sig["level"].value_counts().to_dict().items()},
        "by_model": {str(k): int(v) for k, v in sig["model"].value_counts().to_dict().items()},
        "first_signal_time": iso_ms(int(sig["decision_time"].min())),
    }


def load_15m_candles(db_path: Path, symbol: str, start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], str]:
    if not db_path.exists():
        return [], "missing_db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT open_time, close_time, open, high, low, close, volume, quote_volume, taker_buy_quote
            FROM candles
            WHERE symbol=? AND interval='1m' AND open_time>=? AND open_time<=?
            ORDER BY open_time""",
            (symbol.upper(), int(start_ms), int(end_ms)),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return [], "no_db_candles"
    df = pd.DataFrame([dict(r) for r in rows])
    df["bucket"] = (df["open_time"].astype("int64") // MS_15M) * MS_15M
    agg = (
        df.groupby("bucket", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            quote_volume=("quote_volume", "sum"),
            taker_buy_quote=("taker_buy_quote", "sum"),
        )
        .sort_values("bucket")
    )
    candles = []
    for row in agg.itertuples(index=False):
        candles.append(
            {
                "time": iso_ms(int(row.bucket)),
                "ts": int(row.bucket),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.quote_volume),
            }
        )
    return candles, "db_1m_aggregated_to_15m"


def synthetic_candles(group: pd.DataFrame) -> list[dict[str, Any]]:
    close = group["current_price"].to_numpy(float)
    times = group["decision_time"].to_numpy("int64")
    candles: list[dict[str, Any]] = []
    previous = close[0]
    for ts, price in zip(times, close):
        op = previous
        high = max(op, price)
        low = min(op, price)
        candles.append(
            {
                "time": iso_ms(int(ts)),
                "ts": int(ts),
                "open": float(op),
                "high": float(high),
                "low": float(low),
                "close": float(price),
                "volume": 0.0,
            }
        )
        previous = price
    return candles


def render_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>生命周期信号可视化</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: rgba(255,255,255,0.88);
      --line: #dbe3ef;
      --text: #172033;
      --muted: #667085;
      --blue: #2563eb;
      --green: #059669;
      --red: #dc2626;
      --amber: #d97706;
      --violet: #7c3aed;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 20% 0%, rgba(37,99,235,0.12), transparent 30%),
        radial-gradient(circle at 85% 5%, rgba(5,150,105,0.10), transparent 28%),
        var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px 12px;
      border-bottom: 1px solid rgba(219,227,239,0.75);
      background: rgba(245,247,251,0.72);
      backdrop-filter: blur(16px);
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    .sub {{ color: var(--muted); font-size: 13px; display: flex; gap: 14px; flex-wrap: wrap; }}
    main {{ padding: 18px 28px 28px; }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(260px, 520px) repeat(3, minmax(120px, 1fr));
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }}
    select, button {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.92);
      color: var(--text);
      border-radius: 8px;
      height: 38px;
      padding: 0 12px;
      font-size: 14px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid rgba(219,227,239,0.9);
      border-radius: 8px;
      box-shadow: 0 10px 26px rgba(15,23,42,0.06);
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .metric {{ padding: 12px; }}
    .metric .k {{ color: var(--muted); font-size: 12px; margin-bottom: 5px; }}
    .metric .v {{ font-size: 16px; font-weight: 700; overflow-wrap: anywhere; }}
    #chart {{ height: 720px; }}
    .below {{
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 14px;
      margin-top: 14px;
    }}
    .panel-title {{ padding: 12px 14px 0; font-weight: 700; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    .table-wrap {{ max-height: 360px; overflow: auto; padding: 0 4px 8px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 8px;
      border: 1px solid var(--line);
      background: white;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .short {{ color: var(--red); }}
    .top {{ color: var(--violet); }}
    .warn {{ color: var(--amber); }}
    .entry {{ color: var(--green); }}
    .note {{ color: var(--muted); font-size: 12px; padding: 10px 14px 14px; line-height: 1.6; }}
    @media (max-width: 980px) {{
      .toolbar, .cards, .below {{ grid-template-columns: 1fr; }}
      main, header {{ padding-left: 14px; padding-right: 14px; }}
      #chart {{ height: 560px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>生命周期信号可视化</h1>
    <div class="sub">
      <span id="strategy"></span>
      <span id="generated"></span>
      <span>标注：入池、router、派发预警、见顶、做空</span>
    </div>
  </header>
  <main>
    <div class="toolbar">
      <select id="lifeSelect"></select>
      <button id="prevBtn">上一段</button>
      <button id="nextBtn">下一段</button>
      <button id="resetBtn">重置缩放</button>
    </div>
    <section class="cards">
      <div class="metric card"><div class="k">合约</div><div class="v" id="mSymbol"></div></div>
      <div class="metric card"><div class="k">类型</div><div class="v" id="mFamily"></div></div>
      <div class="metric card"><div class="k">最高涨幅</div><div class="v" id="mGain"></div></div>
      <div class="metric card"><div class="k">信号数</div><div class="v" id="mSignals"></div></div>
      <div class="metric card"><div class="k">K线来源</div><div class="v" id="mSource"></div></div>
    </section>
    <section class="card">
      <div id="chart"></div>
      <div class="note">
        注：正式推送信号是“见顶 early_alert”和“做空 short_signal”；“派发预警 distribution_warning”是内部状态，当前服务器默认不推送。入池点是生命周期起点，不等同于每次正式做多推送。
      </div>
    </section>
    <section class="below">
      <div class="card">
        <div class="panel-title">信号标记</div>
        <div class="table-wrap"><table id="signalTable"></table></div>
      </div>
      <div class="card">
        <div class="panel-title">Router 末端状态</div>
        <div class="table-wrap"><table id="routeTable"></table></div>
      </div>
    </section>
  </main>
  <script id="payload" type="application/json">{data}</script>
  <script>
    const payload = JSON.parse(document.getElementById('payload').textContent);
    const charts = payload.charts || [];
    const select = document.getElementById('lifeSelect');
    let current = 0;

    const colors = {{
      lifecycle_entry: '#059669',
      short_signal: '#dc2626',
      early_alert: '#7c3aed',
      distribution_warning: '#d97706',
      unknown: '#94a3b8',
      fast_dump: '#ef4444',
      slow_distribution: '#d97706',
      second_distribution: '#0ea5e9',
      continuation: '#10b981'
    }};
    const symbols = {{
      lifecycle_entry: 'triangle-up',
      short_signal: 'triangle-down',
      early_alert: 'star',
      distribution_warning: 'diamond'
    }};
    const labels = {{
      lifecycle_entry: '入池',
      short_signal: '做空',
      early_alert: '见顶',
      distribution_warning: '派发'
    }};

    function fmtPct(v) {{
      return v === null || v === undefined || Number.isNaN(Number(v)) ? '-' : (Number(v) * 100).toFixed(1) + '%';
    }}
    function fmtNum(v, n = 4) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return '-';
      return Number(v).toPrecision(n);
    }}
    function compactTime(s) {{
      const d = new Date(s);
      if (Number.isNaN(d.getTime())) return s || '-';
      return d.toLocaleString('zh-CN', {{ hour12: false }});
    }}
    function signalClass(level) {{
      if (level === 'short_signal') return 'short';
      if (level === 'early_alert') return 'top';
      if (level === 'distribution_warning') return 'warn';
      return 'entry';
    }}
    function setHeader() {{
      const s = payload.strategy || {{}};
      document.getElementById('strategy').textContent =
        `${{s.version || '-'}} | router=${{s.router || '-'}} | long=${{s.long_interval || '-'}} | top/short=${{s.expert_interval || '-'}}`;
      document.getElementById('generated').textContent = `生成：${{compactTime(payload.generated_at)}}`;
    }}
    function fillSelect() {{
      charts.forEach((c, i) => {{
        const opt = document.createElement('option');
        opt.value = String(i);
        opt.textContent = `${{i + 1}}. ${{c.symbol}} | ${{c.family}} | gain ${{c.max_gain_pct}}% | signals ${{c.summary?.signals || 0}}`;
        select.appendChild(opt);
      }});
      select.addEventListener('change', () => draw(Number(select.value || 0)));
      document.getElementById('prevBtn').onclick = () => draw(Math.max(0, current - 1));
      document.getElementById('nextBtn').onclick = () => draw(Math.min(charts.length - 1, current + 1));
      document.getElementById('resetBtn').onclick = () => Plotly.relayout('chart', {{ 'xaxis.autorange': true, 'yaxis.autorange': true }});
    }}
    function signalTrace(chart, level) {{
      const rows = chart.signals.filter(s => s.level === level);
      return {{
        type: 'scatter',
        mode: 'markers+text',
        name: labels[level] || level,
        x: rows.map(s => s.time),
        y: rows.map(s => s.price),
        text: rows.map(s => labels[level] || level),
        textposition: level === 'short_signal' ? 'bottom center' : 'top center',
        hovertext: rows.map(s => s.hover),
        hoverinfo: 'text',
        marker: {{
          color: colors[level] || '#111827',
          size: level === 'lifecycle_entry' ? 11 : 14,
          symbol: symbols[level] || 'circle',
          line: {{ color: '#ffffff', width: 1 }}
        }}
      }};
    }}
    function routeTrace(chart) {{
      const rows = chart.routes || [];
      return {{
        type: 'scatter',
        mode: 'markers',
        name: 'router状态',
        x: rows.map(r => r.time),
        y: rows.map(r => r.price),
        yaxis: 'y',
        hovertext: rows.map(r =>
          `route=${{r.route_mode}}<br>candidate=${{r.route_candidate}}<br>state=${{r.behavior_state}}<br>` +
          `conf=${{r.route_confidence.toFixed(3)}} margin=${{r.route_margin.toFixed(3)}} streak=${{r.route_streak}}<br>` +
          `fast=${{r.p_fast.toFixed(3)}} slow=${{r.p_slow.toFixed(3)}} second=${{r.p_second.toFixed(3)}} cont=${{r.p_continuation.toFixed(3)}}<br>` +
          compactTime(r.time)
        ),
        hoverinfo: 'text',
        marker: {{
          color: rows.map(r => colors[r.route_mode] || colors.unknown),
          size: 5,
          opacity: 0.55,
          symbol: 'square'
        }}
      }};
    }}
    function draw(index) {{
      current = index;
      select.value = String(index);
      const chart = charts[index];
      if (!chart) return;
      document.getElementById('mSymbol').textContent = chart.symbol;
      document.getElementById('mFamily').textContent = chart.family || '-';
      document.getElementById('mGain').textContent = chart.max_gain_pct + '%';
      document.getElementById('mSignals').textContent = chart.summary?.signals || 0;
      document.getElementById('mSource').textContent = chart.candles_source;

      const candles = chart.candles || [];
      const traces = [
        {{
          type: 'candlestick',
          name: chart.symbol,
          x: candles.map(c => c.time),
          open: candles.map(c => c.open),
          high: candles.map(c => c.high),
          low: candles.map(c => c.low),
          close: candles.map(c => c.close),
          increasing: {{ line: {{ color: '#059669' }}, fillcolor: '#059669' }},
          decreasing: {{ line: {{ color: '#dc2626' }}, fillcolor: '#dc2626' }},
          yaxis: 'y'
        }},
        {{
          type: 'bar',
          name: 'quote volume',
          x: candles.map(c => c.time),
          y: candles.map(c => c.volume),
          marker: {{ color: 'rgba(37,99,235,0.22)' }},
          yaxis: 'y2'
        }},
        routeTrace(chart),
        signalTrace(chart, 'lifecycle_entry'),
        signalTrace(chart, 'distribution_warning'),
        signalTrace(chart, 'early_alert'),
        signalTrace(chart, 'short_signal')
      ];
      const layout = {{
        margin: {{ l: 52, r: 28, t: 26, b: 38 }},
        paper_bgcolor: 'rgba(255,255,255,0)',
        plot_bgcolor: 'rgba(255,255,255,0)',
        showlegend: true,
        legend: {{ orientation: 'h', x: 0, y: 1.08 }},
        xaxis: {{ rangeslider: {{ visible: false }}, gridcolor: '#eef2f7' }},
        yaxis: {{ title: 'price', domain: [0.24, 1], gridcolor: '#eef2f7', fixedrange: false }},
        yaxis2: {{ title: 'volume', domain: [0, 0.16], gridcolor: '#eef2f7', fixedrange: true }},
        hovermode: 'closest'
      }};
      Plotly.newPlot('chart', traces, layout, {{ responsive: true, displaylogo: false }});
      fillSignalTable(chart);
      fillRouteTable(chart);
    }}
    function fillSignalTable(chart) {{
      const rows = chart.signals || [];
      const html = [
        '<thead><tr><th>时间</th><th>信号</th><th>价格</th><th>模型</th><th>分数</th><th>路线/状态</th><th>后续</th></tr></thead>',
        '<tbody>',
        ...rows.map(s => `<tr>
          <td>${{compactTime(s.time)}}</td>
          <td><span class="badge ${{signalClass(s.level)}}">${{s.label}}</span></td>
          <td>${{fmtNum(s.price)}}</td>
          <td>${{s.model || '-'}}</td>
          <td>${{s.score == null ? '-' : s.score.toFixed(3)}} / ${{s.threshold == null ? '-' : s.threshold.toFixed(3)}}</td>
          <td>${{s.route_mode || '-'}}<br><span class="subtle">${{s.behavior_state || ''}}</span></td>
          <td>跌6h ${{fmtPct(s.future_drop_6h)}}<br>跌24h ${{fmtPct(s.future_drop_24h)}} / 逆24h ${{fmtPct(s.short_adverse_24h)}}</td>
        </tr>`),
        '</tbody>'
      ].join('');
      document.getElementById('signalTable').innerHTML = html;
    }}
    function fillRouteTable(chart) {{
      const rows = (chart.routes || []).slice(-24).reverse();
      const html = [
        '<thead><tr><th>时间</th><th>确认路线</th><th>候选/状态</th><th>概率</th></tr></thead>',
        '<tbody>',
        ...rows.map(r => `<tr>
          <td>${{compactTime(r.time)}}</td>
          <td><span class="badge">${{r.route_mode}}</span><br>conf ${{r.route_confidence.toFixed(3)}} / margin ${{r.route_margin.toFixed(3)}} / streak ${{r.route_streak}}</td>
          <td>${{r.route_candidate}}<br>${{r.behavior_state}}</td>
          <td>fast ${{r.p_fast.toFixed(3)}}<br>slow ${{r.p_slow.toFixed(3)}}<br>second ${{r.p_second.toFixed(3)}}<br>cont ${{r.p_continuation.toFixed(3)}}</td>
        </tr>`),
        '</tbody>'
      ].join('');
      document.getElementById('routeTable').innerHTML = html;
    }}
    setHeader();
    fillSelect();
    draw(0);
  </script>
</body>
</html>"""


def serve_html(out_path: Path, host: str, port: int, open_browser: bool = True) -> str:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out_path.parent))
    server = http.server.ThreadingHTTPServer((host, int(port)), handler)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/{out_path.name}"
    if open_browser:
        webbrowser.open(url)
    print(f"serving {out_path.parent} at {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("server stopped", flush=True)
    finally:
        server.server_close()
    return url


def signal_label(level: str) -> str:
    return {
        "lifecycle_entry": "生命周期启动/入池",
        "short_signal": "做空",
        "early_alert": "见顶",
        "distribution_warning": "派发预警(内部)",
    }.get(level, level)


def pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "-"


def none_or_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def iso_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
