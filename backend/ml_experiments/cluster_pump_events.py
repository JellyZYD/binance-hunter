"""Cluster historical altcoin pump-to-afterpeak events.

This experiment is intentionally upstream of long/top/dump models. It extracts
complete pump events first, clusters their realized paths, and writes a
taxonomy report that can drive per-regime signal/model design later.

It does not modify production models.

Example:
    python ml_experiments/cluster_pump_events.py --source "E:\\2C2G\\币安数据库" --days 0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from ml_experiments.train_dump_5m_compare import (
    Variant,
    aggregate,
    data_end,
    iso_ms,
    parquet_files,
    compute_features_interval,
)
from ml_experiments.train_top_low_adverse import data_start
from pump_dump_hunter.ml.train import DAY

VARIANT_15M = Variant("15m", 15 * 60_000, "native")
HOUR = 3_600_000
BAR_HOURS = 0.25


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=r"E:\2C2G\币安数据库")
    ap.add_argument("--days", type=int, default=0, help="0 means all available history.")
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--out", default="storage/ml/pump_event_taxonomy.json")
    ap.add_argument("--report", default="storage/ml/pump_event_taxonomy.md")
    ap.add_argument("--events", default="storage/ml/pump_events_clustered.parquet")
    args = ap.parse_args(argv)

    source = Path(args.source)
    files = parquet_files(source, args.max_symbols)
    end = data_end(files)
    start = data_start(files) if args.days <= 0 else end - args.days * DAY
    events: list[dict[str, Any]] = []
    for i, path in enumerate(files, 1):
        sym = path.stem.upper()
        try:
            g = aggregate(path, start - 5 * DAY, end, VARIANT_15M)
        except Exception as exc:
            print(f"skip {sym}: {exc}", flush=True)
            continue
        if g is None or len(g) < 500:
            continue
        events.extend(extract_symbol_events(sym, g))
        if i % 25 == 0:
            print(f"loaded {i}/{len(files)} events={len(events)}", flush=True)

    df = pd.DataFrame(events)
    if df.empty:
        raise SystemExit("no events extracted")
    df = df.sort_values(["start_time", "symbol"]).reset_index(drop=True)
    clustered, diagnostics = cluster_events(df)
    summaries = summarize_clusters(clustered)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": str(source),
        "data_start": iso_ms(start),
        "data_end": iso_ms(end),
        "symbols_total": len(files),
        "events": int(len(clustered)),
        "symbols_with_events": int(clustered.symbol.nunique()),
        "diagnostics": diagnostics,
        "cluster_summaries": summaries,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.events).parent.mkdir(parents=True, exist_ok=True)
    clustered.to_parquet(args.events, index=False)
    Path(args.report).write_text(render_report(payload), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def extract_symbol_events(symbol: str, g: pd.DataFrame) -> list[dict[str, Any]]:
    f = compute_features_interval(g, VARIANT_15M)
    close = g.close.values
    high = g.high.values
    low = g.low.values
    qv = g.qv.values
    tbq = g.tbq.values
    n = len(g)
    ret_16 = pd.Series(close).div(pd.Series(close).shift(16)).sub(1.0).values
    trigger = (
        (ret_16 >= 0.18)
        | (f["ret_48"].values >= 0.28)
        | (f["ret_96"].values >= 0.40)
        | ((f["runup_96"].values >= 0.35) & (f["ret_6"].values >= 0.05))
    )
    trigger &= np.isfinite(f["ret_96"].values)
    idxs = np.where(trigger)[0]
    out: list[dict[str, Any]] = []
    cursor = 120
    for trig in idxs:
        if trig < cursor:
            continue
        start_lo = max(0, trig - 96)
        start_ix = start_lo + int(np.argmin(low[start_lo : trig + 1]))
        peak_end = min(n, trig + 193)
        if peak_end <= trig + 4:
            continue
        peak_ix = trig + int(np.argmax(high[trig:peak_end]))
        peak_price = float(high[peak_ix])
        start_price = float(low[start_ix])
        pump_ret = peak_price / start_price - 1.0
        if pump_ret < 0.20:
            cursor = trig + 8
            continue
        post_end = min(n - 1, peak_ix + 288)
        if post_end <= peak_ix + 24:
            continue

        post_hi = high[peak_ix + 1 : post_end + 1]
        post_lo = low[peak_ix + 1 : post_end + 1]
        post_close = close[peak_ix + 1 : post_end + 1]
        drop_times = {p: first_drop_hours(post_lo, peak_price, p) for p in (0.05, 0.10, 0.15, 0.20, 0.30, 0.40)}
        max_drop_2h = max_drop(post_lo, peak_price, bars=8)
        max_drop_6h = max_drop(post_lo, peak_price, bars=24)
        max_drop_12h = max_drop(post_lo, peak_price, bars=48)
        max_drop_24h = max_drop(post_lo, peak_price, bars=96)
        max_drop_72h = float(peak_price / np.min(post_lo) - 1.0) if len(post_lo) else 0.0
        future_up72 = float(np.max(post_hi) / peak_price - 1.0) if len(post_hi) else 0.0
        end_ret72 = float(post_close[-1] / peak_price - 1.0) if len(post_close) else 0.0
        near_peak_24 = high_share(post_close[:96], peak_price, threshold=0.95)
        below_95_h = first_close_below_hours(post_close, peak_price * 0.95)
        retests = retest_count(post_hi, peak_price)

        pre_start = max(0, start_ix - 96)
        pre_qv = safe_mean(qv[pre_start:start_ix])
        trigger_qv = safe_mean(qv[max(0, trig - 16) : trig + 1])
        pump_qv = safe_mean(qv[start_ix : peak_ix + 1])
        qv_ratio_trigger = trigger_qv / pre_qv if pre_qv > 0 else np.nan
        qv_ratio_pump = pump_qv / pre_qv if pre_qv > 0 else np.nan
        taker_sell_trigger = taker_sell(qv[trig], tbq[trig])
        taker_sell_peak = taker_sell(qv[peak_ix], tbq[peak_ix])
        pre_retstd = pd.Series(close[max(1, start_ix - 96) : start_ix + 1]).pct_change().std()

        row = {
            "symbol": symbol,
            "start_time": int(g.b.iloc[start_ix]),
            "trigger_time": int(g.b.iloc[trig]),
            "peak_time": int(g.b.iloc[peak_ix]),
            "post_end_time": int(g.b.iloc[post_end]),
            "start_time_iso": iso_ms(int(g.b.iloc[start_ix])),
            "trigger_time_iso": iso_ms(int(g.b.iloc[trig])),
            "peak_time_iso": iso_ms(int(g.b.iloc[peak_ix])),
            "start_ix": int(start_ix),
            "trigger_ix": int(trig),
            "peak_ix": int(peak_ix),
            "pump_ret": pump_ret,
            "start_to_trigger_h": hours_between(g.b.iloc[start_ix], g.b.iloc[trig]),
            "trigger_to_peak_h": hours_between(g.b.iloc[trig], g.b.iloc[peak_ix]),
            "start_to_peak_h": hours_between(g.b.iloc[start_ix], g.b.iloc[peak_ix]),
            "pump_speed_per_h": pump_ret / max(hours_between(g.b.iloc[start_ix], g.b.iloc[peak_ix]), 0.25),
            "trigger_ret_4h": safe_float(ret_16[trig]),
            "trigger_ret_12h": safe_float(f["ret_48"].iloc[trig]),
            "trigger_ret_24h": safe_float(f["ret_96"].iloc[trig]),
            "trigger_runup_24h": safe_float(f["runup_96"].iloc[trig]),
            "trigger_dd_24h": safe_float(f["dd_96"].iloc[trig]),
            "trigger_close_pos": safe_float(f["close_pos"].iloc[trig]),
            "trigger_uwick": safe_float(f["uwick"].iloc[trig]),
            "trigger_volr": safe_float(f["volr_20"].iloc[trig]),
            "trigger_taker_sell": taker_sell_trigger,
            "peak_close_pos": safe_float(f["close_pos"].iloc[peak_ix]),
            "peak_uwick": safe_float(f["uwick"].iloc[peak_ix]),
            "peak_volr": safe_float(f["volr_20"].iloc[peak_ix]),
            "peak_taker_sell": taker_sell_peak,
            "qv_ratio_trigger": qv_ratio_trigger,
            "qv_ratio_pump": qv_ratio_pump,
            "pre_retstd_24h": safe_float(pre_retstd),
            "near_peak_share_24h": near_peak_24,
            "first_close_below_95_h": below_95_h,
            "retest_count_72h": retests,
            "drop5_h": drop_times[0.05],
            "drop10_h": drop_times[0.10],
            "drop15_h": drop_times[0.15],
            "drop20_h": drop_times[0.20],
            "drop30_h": drop_times[0.30],
            "drop40_h": drop_times[0.40],
            "max_drop_2h": max_drop_2h,
            "max_drop_6h": max_drop_6h,
            "max_drop_12h": max_drop_12h,
            "max_drop_24h": max_drop_24h,
            "max_drop_72h": max_drop_72h,
            "future_up72": future_up72,
            "end_ret72": end_ret72,
        }
        out.append(row)
        cursor = max(post_end, trig + 96)
    return out


def cluster_events(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    cluster_features = [
        "pump_ret",
        "start_to_peak_h",
        "trigger_to_peak_h",
        "pump_speed_per_h",
        "trigger_ret_4h",
        "trigger_ret_24h",
        "qv_ratio_trigger",
        "qv_ratio_pump",
        "trigger_volr",
        "peak_volr",
        "trigger_taker_sell",
        "peak_taker_sell",
        "near_peak_share_24h",
        "first_close_below_95_h",
        "retest_count_72h",
        "drop5_h",
        "drop15_h",
        "drop20_h",
        "max_drop_6h",
        "max_drop_24h",
        "max_drop_72h",
        "future_up72",
        "end_ret72",
        "pre_retstd_24h",
    ]
    x = df[cluster_features].copy()
    for col in x.columns:
        x[col] = pd.to_numeric(x[col], errors="coerce")
        hi = x[col].quantile(0.99)
        lo = x[col].quantile(0.01)
        x[col] = x[col].clip(lo, hi)
        x[col] = x[col].fillna(x[col].median())
    scaler = StandardScaler()
    xs = scaler.fit_transform(x)
    diagnostics: dict[str, Any] = {"features": cluster_features, "k_scores": {}}
    best_k = 6
    best_score = -1.0
    for k in range(4, 10):
        model = KMeans(n_clusters=k, random_state=42, n_init=30)
        labels = model.fit_predict(xs)
        score = float(silhouette_score(xs, labels))
        diagnostics["k_scores"][str(k)] = round(score, 4)
        if score > best_score:
            best_score = score
            best_k = k
    model = KMeans(n_clusters=best_k, random_state=42, n_init=50)
    labels = model.fit_predict(xs)
    out = df.copy()
    out["cluster"] = labels
    diagnostics["selected_k"] = best_k
    diagnostics["selected_silhouette"] = round(best_score, 4)
    return out, diagnostics


def summarize_clusters(df: pd.DataFrame) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    overall = metric_medians(df)
    for cluster, grp in df.groupby("cluster"):
        med = metric_medians(grp)
        name = name_cluster(med)
        summary = {
            "cluster": int(cluster),
            "name": name,
            "count": int(len(grp)),
            "pct": round(float(len(grp) / len(df)), 4),
            "symbols": int(grp.symbol.nunique()),
            "period_start": iso_ms(int(grp.start_time.min())),
            "period_end": iso_ms(int(grp.start_time.max())),
            "metrics": {k: rounded(v) for k, v in med.items()},
            "signature": signature(med, overall),
            "strategy_notes": strategy_notes(name, med),
        }
        summaries.append(summary)
    summaries.sort(key=lambda x: x["count"], reverse=True)
    return summaries


def metric_medians(df: pd.DataFrame) -> dict[str, float]:
    metrics = [
        "pump_ret",
        "start_to_peak_h",
        "trigger_to_peak_h",
        "pump_speed_per_h",
        "trigger_ret_4h",
        "trigger_ret_24h",
        "qv_ratio_trigger",
        "near_peak_share_24h",
        "first_close_below_95_h",
        "retest_count_72h",
        "drop5_h",
        "drop15_h",
        "drop20_h",
        "max_drop_6h",
        "max_drop_24h",
        "max_drop_72h",
        "future_up72",
        "end_ret72",
        "trigger_taker_sell",
        "peak_taker_sell",
    ]
    out = {m: float(pd.to_numeric(df[m], errors="coerce").median()) for m in metrics}
    out["hit15_72h_rate"] = float((df.max_drop_72h >= 0.15).mean())
    out["hit20_72h_rate"] = float((df.max_drop_72h >= 0.20).mean())
    out["quick_drop15_6h_rate"] = float((df.drop15_h <= 6).mean())
    out["quick_drop20_12h_rate"] = float((df.drop20_h <= 12).mean())
    out["continuation_rate"] = float(((df.future_up72 >= 0.10) & (df.max_drop_24h < 0.12)).mean())
    return out


def name_cluster(m: dict[str, float]) -> str:
    if (
        m["continuation_rate"] >= 0.35
        or m["max_drop_72h"] < 0.12
        or (m["near_peak_share_24h"] >= 0.65 and m["hit15_72h_rate"] < 0.20)
    ):
        return "持续拉升/不宜做空"
    if m["quick_drop20_12h_rate"] >= 0.45 and m["drop20_h"] <= 12:
        return "急拉急砸"
    if m["near_peak_share_24h"] >= 0.45 and m["hit20_72h_rate"] >= 0.45:
        return "高位横盘后派发"
    if m["drop15_h"] >= 12 and m["hit20_72h_rate"] >= 0.35:
        return "慢跌出货"
    if m["pump_ret"] >= 0.80 and m["max_drop_72h"] >= 0.30:
        return "大妖高波动派发"
    if m["future_up72"] >= 0.15 and m["max_drop_72h"] >= 0.20:
        return "宽幅震荡洗盘"
    return "普通冲高回落"


def signature(m: dict[str, float], overall: dict[str, float]) -> list[str]:
    candidates = []
    for key, value in m.items():
        base = overall.get(key)
        if base is None or not np.isfinite(base):
            continue
        diff = value - base
        if abs(diff) < 1e-9:
            continue
        candidates.append((abs(diff) / (abs(base) + 1e-6), key, value, base))
    candidates.sort(reverse=True)
    out = []
    for _, key, value, base in candidates[:6]:
        direction = "高" if value > base else "低"
        out.append(f"{key} {direction}: {rounded(value)} vs overall {rounded(base)}")
    return out


def strategy_notes(name: str, m: dict[str, float]) -> dict[str, str]:
    if name == "持续拉升/不宜做空":
        return {
            "bias": "偏做多或观望空头",
            "early": "启动后72h仍容易续高，顶部/做空模型应降权。",
            "long": "做多模型优先跟踪趋势延续和加仓，不急于找顶。",
            "top": "只有出现明确跌破结构且回抽失败后再考虑。",
            "short": "不做第一顶部，等待真实下跌启动确认。",
        }
    if name == "急拉急砸":
        return {
            "bias": "偏快速做空",
            "early": "短时间大涨、量能突增、峰后很快跌破5/10%。",
            "long": "只能做极短启动段，后续要很快进入止盈/警戒。",
            "top": "5m/15m 冲高失败或长上影要高权重。",
            "short": "适合 short_fast，必须吃速度，不适合等很久。",
        }
    if name == "高位横盘后派发":
        return {
            "bias": "等待顶部区间破位",
            "early": "峰后24h仍贴近高位，多次回测峰值。",
            "long": "横盘不破时可以只保留观察，不盲目追多。",
            "top": "重点识别高位横盘、量能衰减、上冲失败。",
            "short": "破横盘实体低点 + 放量是主信号。",
        }
    if name == "慢跌出货":
        return {
            "bias": "偏趋势跟踪空，不抢顶",
            "early": "顶部后先横向或缓慢下移，急跌不明显。",
            "long": "趋势减速后减少做多权重。",
            "top": "顶信号只做减仓/观察，不直接重仓开空。",
            "short": "等15m/1h结构转弱，适合拿72h。",
        }
    if name == "宽幅震荡洗盘":
        return {
            "bias": "降低自动信号置信",
            "early": "峰后既能大跌也能再冲高，路径锯齿强。",
            "long": "只做明确强趋势段。",
            "top": "顶部信号需要更严格的再涨过滤。",
            "short": "必须小仓或等二次确认，避免被回抽打穿。",
        }
    return {
        "bias": "普通短线跟随",
        "early": "常规暴涨后回落，边际不如极端类型清晰。",
        "long": "启动段可做，但需要快速验证。",
        "top": "作为普通预警，不做强信号。",
        "short": "等待 dump/short 确认优于抢顶。",
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Pump Event Taxonomy",
        "",
        f"Data: {payload['data_start']} to {payload['data_end']}",
        f"Events: {payload['events']} across {payload['symbols_with_events']} symbols",
        f"Selected k: {payload['diagnostics']['selected_k']} silhouette={payload['diagnostics']['selected_silhouette']}",
        "",
        "## Cluster Summary",
        "",
        "| Cluster | Name | Count | Pump% med | Peak h med | Drop20 h med | Max drop72 med | Future up72 med | Hit20 | Continuation |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in payload["cluster_summaries"]:
        m = s["metrics"]
        lines.append(
            f"| {s['cluster']} | {s['name']} | {s['count']} | {pct(m['pump_ret'])} | "
            f"{m['start_to_peak_h']} | {m['drop20_h']} | {pct(m['max_drop_72h'])} | "
            f"{pct(m['future_up72'])} | {pct(m['hit20_72h_rate'])} | {pct(m['continuation_rate'])} |"
        )
    lines += ["", "## Strategy Notes", ""]
    for s in payload["cluster_summaries"]:
        lines += [
            f"### Cluster {s['cluster']} - {s['name']}",
            f"- Count: {s['count']} ({pct(s['pct'])})",
            f"- Signature: {'; '.join(s['signature'])}",
        ]
        notes = s["strategy_notes"]
        for key in ("bias", "early", "long", "top", "short"):
            lines.append(f"- {key}: {notes[key]}")
        lines.append("")
    return "\n".join(lines)


def first_drop_hours(lows: np.ndarray, peak: float, drop: float) -> float:
    if len(lows) == 0:
        return 72.0
    hit = np.where(lows <= peak * (1.0 - drop))[0]
    return float((hit[0] + 1) * BAR_HOURS) if len(hit) else 72.0


def first_close_below_hours(closes: np.ndarray, level: float) -> float:
    if len(closes) == 0:
        return 72.0
    hit = np.where(closes <= level)[0]
    return float((hit[0] + 1) * BAR_HOURS) if len(hit) else 72.0


def max_drop(lows: np.ndarray, peak: float, bars: int) -> float:
    if len(lows) == 0:
        return 0.0
    seg = lows[: min(len(lows), bars)]
    return float(peak / np.min(seg) - 1.0)


def high_share(closes: np.ndarray, peak: float, threshold: float) -> float:
    if len(closes) == 0:
        return 0.0
    return float((closes >= peak * threshold).mean())


def retest_count(highs: np.ndarray, peak: float) -> int:
    if len(highs) == 0:
        return 0
    near = highs >= peak * 0.97
    count = 0
    in_run = False
    for value in near:
        if value and not in_run:
            count += 1
            in_run = True
        elif not value:
            in_run = False
    return int(count)


def taker_sell(qv: float, tbq: float) -> float:
    if not qv or qv <= 0:
        return float("nan")
    return float(1.0 - tbq / qv)


def safe_mean(values: np.ndarray) -> float:
    if len(values) == 0:
        return float("nan")
    return float(np.nanmean(values))


def safe_float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def hours_between(a: Any, b: Any) -> float:
    return float((int(b) - int(a)) / HOUR)


def rounded(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not np.isfinite(out):
        return None
    return round(out, 4)


def pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return ""


if __name__ == "__main__":
    main()
