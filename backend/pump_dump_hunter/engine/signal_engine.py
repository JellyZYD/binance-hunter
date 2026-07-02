from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..indicators import ema, mean, pct_change, safe_div
from ..models import Alert, Candle, KlineClosed, LiquidityRecord, LongEvent, PumpEvent, SignalParams
from ..timeutils import interval_to_ms


class SignalEngine:
    def __init__(self, settings: dict[str, Any], params: SignalParams | None = None):
        self.settings = settings
        self.params = params or SignalParams.from_dict(settings["params"])
        self.active_hours = float(settings["discovery"]["active_hours"])
        self.signal_cfg = settings["signals"]
        self.early_interval = str(self.signal_cfg.get("early_interval", "5m"))
        self.confirm_interval = str(self.signal_cfg.get("confirm_interval", "15m"))
        self.mode = str(self.signal_cfg.get("mode", "legacy"))
        self.ml = None
        if self.mode == "ml":
            try:
                from ..ml.model import MLScorer
                self.ml = MLScorer()
                print(f"ML scorer ready={self.ml.ready} info={self.ml.info().get('trained_at', self.ml.error)}", flush=True)
            except Exception as exc:
                print(f"ML scorer load failed: {exc}", flush=True)
                self.ml = None
        self.events_by_symbol: dict[str, PumpEvent] = {}
        # 做多线(纯提示; 需 mode="ml" + long_enabled)
        self.long_enabled = bool(self.signal_cfg.get("long_enabled", False))
        self.long_watch_hours = float(self.signal_cfg.get("long_watch_hours", 36.0))
        self.long_trend_break_pct = float(self.signal_cfg.get("long_trend_break_pct", 8.0))
        self.long_events_by_symbol: dict[str, LongEvent] = {}
        self.flow_cache: dict[str, Any] = {}  # symbol -> 资金流 DataFrame(ts,oi,oival,lsg,lstp,tkr), 每15m刷新
        self.buffers: dict[tuple[str, str], deque[Candle]] = defaultdict(lambda: deque(maxlen=240))
        self.buffer_times: dict[tuple[str, str], set[int]] = defaultdict(set)

    def load_events(self, events: list[PumpEvent]) -> None:
        for event in events:
            self.events_by_symbol[event.symbol] = event

    def load_long_events(self, events: list[LongEvent]) -> None:
        for event in events:
            self.long_events_by_symbol[event.symbol] = event

    def on_discovery(self, records: list[LiquidityRecord], decision_time: int) -> list[PumpEvent]:
        changed = []
        active_ms = int(self.active_hours * 3_600_000)
        for record in records:
            if self.long_enabled and record.long_candidate:
                self._upsert_long_event(record, decision_time)
            if not record.pump_qualified:
                continue
            existing = self.events_by_symbol.get(record.symbol)
            anchor_price = infer_anchor_price(record)
            strongest_pct, trigger_window = strongest_pump_window(record)
            high_price = max(record.last_price, anchor_price * (1.0 + strongest_pct / 100.0))
            evidence = [
                f"liquidity_rank={record.rank}",
                f"pct_15m={record.pct_15m:+.2f}%",
                f"pct_30m={record.pct_30m:+.2f}%",
                f"pct_4h={record.pct_4h:+.2f}%",
                f"pct_12h={record.pct_12h:+.2f}%",
                f"pct_1d={record.pct_1d:+.2f}%",
                f"qv_15m={record.quote_volume_15m:.0f}",
                f"qv_30m={record.quote_volume_30m:.0f}",
            ]
            if existing and existing.expires_at >= decision_time:
                existing.last_seen = decision_time
                existing.expires_at = max(existing.expires_at, decision_time + active_ms)
                existing.current_price = record.last_price
                if high_price > existing.high_price * (1.0 + self.params.new_high_reset_pct / 100.0):
                    existing.high_price = high_price
                    existing.high_time = decision_time
                    existing.early_alerted_after_high_time = None
                    existing.short_alerted_after_high_time = None
                    existing.fallback_alerted_after_high_time = None
                existing.max_gain_pct = pct_change(existing.anchor_price, existing.high_price)
                existing.evidence = sorted(set(existing.evidence + evidence))
                changed.append(existing)
            else:
                event = PumpEvent(
                    event_id=f"{record.symbol}-{decision_time}",
                    symbol=record.symbol,
                    first_seen=decision_time,
                    last_seen=decision_time,
                    expires_at=decision_time + active_ms,
                    trigger_window=trigger_window,
                    anchor_price=anchor_price,
                    high_price=high_price,
                    high_time=decision_time,
                    current_price=record.last_price,
                    max_gain_pct=pct_change(anchor_price, high_price),
                    evidence=evidence,
                )
                self.events_by_symbol[record.symbol] = event
                changed.append(event)
        return changed

    def _upsert_long_event(self, record: LiquidityRecord, decision_time: int) -> None:
        """入选做多监管; 持续满足则滚动刷新窗口(entry_price 固定为首次入场)。"""
        watch_ms = int(self.long_watch_hours * 3_600_000)
        le = self.long_events_by_symbol.get(record.symbol)
        if le and le.status == "active":
            le.last_seen = decision_time
            le.current_price = record.last_price
            le.high_price = max(le.high_price, record.last_price)
            le.expires_at = decision_time + watch_ms
        else:
            self.long_events_by_symbol[record.symbol] = LongEvent(
                event_id=f"{record.symbol}-L-{decision_time}",
                symbol=record.symbol, first_seen=decision_time, last_seen=decision_time,
                expires_at=decision_time + watch_ms, entry_price=record.last_price,
                high_price=record.last_price, current_price=record.last_price,
                evidence=[f"入选做多: 30m={record.pct_30m:+.2f}% 量比={record.volume_ratio_30m:.1f}x 4h={record.pct_4h:+.2f}%"],
            )

    def active_long_symbols(self) -> list[str]:
        return [s for s, le in self.long_events_by_symbol.items() if le.status == "active"]

    def set_flow(self, symbol: str, flow_df: Any) -> None:
        """由 live 层每 15m 用实时 OI/多空/taker 刷新资金流缓存。"""
        self.flow_cache[symbol] = flow_df

    def _process_long(self, candle: Candle) -> list[Alert]:
        """做多监管: 更新窗口, 判退出(超时/趋势破坏), 出做多信号(long_setup + ML)。"""
        le = self.long_events_by_symbol.get(candle.symbol)
        if le is None or le.status != "active":
            return []
        if candle.close_time > le.expires_at:
            le.status = "closed"; le.exit_reason = "超时"
            return [make_long_status_alert("long_timeout", le, candle, "监管超时", [f"watch_hours={self.long_watch_hours:.0f}"])]
        le.current_price = candle.close
        le.high_price = max(le.high_price, candle.high)
        le.last_seen = candle.close_time
        if candle.close <= le.entry_price * (1.0 - self.long_trend_break_pct / 100.0):
            le.status = "closed"; le.exit_reason = "趋势破坏"
            return [make_long_status_alert("long_invalid", le, candle, "趋势破坏", [f"drop_from_entry={pct_change(candle.close, le.entry_price):+.2f}%"])]
        exit_alerts = self._long_exit_signals(le, candle)
        if exit_alerts:
            return exit_alerts
        return self._long_signals(le, candle)

    def _long_exit_signals(self, le: LongEvent, candle: Candle) -> list[Alert]:
        if candle.interval != self.confirm_interval or self.ml is None or not self.ml.ready:
            return []
        from ..ml import features as mlf
        candles = sorted(self.buffers[(le.symbol, self.confirm_interval)], key=lambda c: c.open_time)
        if len(candles) < mlf.LOOKBACK + 2:
            return []
        row = mlf.compute_features(mlf.candles_to_frame(candles)).iloc[-1]
        if row[self.ml.cols].isna().any():
            return []
        for task, level, setup_fn, tag, reason in (
            ("dump", "short_signal", mlf.dump_setup_flags, "ML破位分", "下跌启动"),
            ("top", "early_alert", mlf.top_setup_flags, "ML见顶分", "见顶"),
        ):
            if not bool(setup_fn(row)):
                continue
            sc = self.ml.score(row, task)
            thr = self.ml.threshold(task)
            if sc is None or thr is None or sc < thr:
                continue
            hi = self.ml.threshold_high(task) or 2.0
            tier = "高置信" if sc >= hi else "普通"
            le.status = "closed"
            le.exit_reason = reason
            category = "平多/做空" if level == "short_signal" else "平多"
            return [make_long_exit_alert(level, le, candle, category, [f"{tag}={sc:.2f}", f"置信={tier}", f"long_exit={reason}"])]
        return []

    def _long_signals(self, le: LongEvent, candle: Candle) -> list[Alert]:
        if candle.interval != self.confirm_interval or self.ml is None or not self.ml.ready:
            return []
        from ..ml import features as mlf
        candles = sorted(self.buffers[(le.symbol, self.confirm_interval)], key=lambda c: c.open_time)
        if len(candles) < mlf.LOOKBACK + 2:
            return []
        df = mlf.candles_to_frame(candles)
        f = mlf.compute_features(df)
        if not bool(mlf.long_setup_flags(df, f).iloc[-1]):
            return []
        row = f.iloc[-1]
        if row[mlf.feature_columns()].isna().any():  # base 特征需齐全(资金流缺省 NaN, LGB 处理)
            return []
        # 资金流特征(实时缓存对齐; 无则 NaN, LGB 原生处理)
        flow_in = mlf.align_flow([c.open_time for c in candles], [c.close for c in candles], self.flow_cache.get(le.symbol))
        flow_row = mlf.compute_flow_features(flow_in).iloc[-1]
        full = row.copy()
        for col in mlf.flow_columns():
            full[col] = flow_row[col]
        sc = self.ml.score(full, "long")
        thr = self.ml.threshold("long")
        if sc is None or thr is None or sc < thr:
            return []
        hi = self.ml.threshold_high("long") or 2.0
        tier = "高置信" if sc >= hi else "普通观察"
        return [make_long_alert(le, candle, self.long_trend_break_pct, [f"ML做多分={sc:.2f}", f"置信={tier}"])]

    def prime_candles(self, candles: list[Candle]) -> list[PumpEvent]:
        changed: dict[str, PumpEvent] = {}
        for candle in sorted(candles, key=lambda c: (c.close_time, c.interval)):
            if not self._append_candle(candle):
                continue
            pump = self.events_by_symbol.get(candle.symbol)
            if not pump or pump.status != "active" or pump.expires_at < candle.close_time:
                continue
            if candle.high > pump.high_price * (1.0 + self.params.new_high_reset_pct / 100.0):
                pump.high_price = candle.high
                pump.high_time = candle.close_time
                pump.early_alerted_after_high_time = None
                pump.short_alerted_after_high_time = None
                pump.expires_at = max(pump.expires_at, candle.close_time + int(self.active_hours * 3_600_000))
            pump.current_price = candle.close
            pump.last_seen = candle.close_time
            pump.max_gain_pct = pct_change(pump.anchor_price, pump.high_price)
            changed[pump.symbol] = pump
        return list(changed.values())
    def on_kline(self, event: KlineClosed) -> tuple[list[PumpEvent], list[Alert]]:
        candle = event.candle
        if not self._append_candle(candle):
            return [], []
        changed: list[PumpEvent] = []
        alerts: list[Alert] = []
        pump = self.events_by_symbol.get(candle.symbol)
        if not pump or pump.status != "active" or pump.expires_at < candle.close_time:
            if self.long_enabled:
                alerts.extend(self._process_long(candle))
            return changed, alerts

        if candle.high > pump.high_price * (1.0 + self.params.new_high_reset_pct / 100.0):
            pump.high_price = candle.high
            pump.high_time = candle.close_time
            pump.early_alerted_after_high_time = None
            pump.short_alerted_after_high_time = None
            pump.fallback_alerted_after_high_time = None
            pump.expires_at = max(pump.expires_at, candle.close_time + int(self.active_hours * 3_600_000))
            changed.append(pump)
        pump.current_price = candle.close
        pump.last_seen = candle.close_time
        pump.max_gain_pct = pct_change(pump.anchor_price, pump.high_price)

        if self.mode == "ml":
            long_exit_triggered = False
            for alert in self._ml_signals(pump, candle):
                if alert.level == "early_alert":
                    pump.early_alerted_after_high_time = pump.high_time
                    le = self.long_events_by_symbol.get(candle.symbol)
                    if le and le.status == "active":  # 见顶 = 平多, 结束做多监管
                        le.status = "closed"; le.exit_reason = "见顶"
                        long_exit_triggered = True
                elif alert.level == "short_signal":
                    pump.short_alerted_after_high_time = pump.high_time
                    le = self.long_events_by_symbol.get(candle.symbol)
                    if le and le.status == "active":  # 下跌启动 = 平多/做空, 结束做多监管
                        le.status = "closed"; le.exit_reason = "下跌启动"
                        long_exit_triggered = True
                alerts.append(alert)
                changed.append(pump)
            if self.long_enabled and not long_exit_triggered:
                alerts.extend(self._process_long(candle))
            return changed, alerts

        if self.mode == "v2":
            long_exit_triggered = False
            if candle.interval == self.early_interval:
                alert = self._early_alert_v2(pump, candle)
                if alert:
                    pump.early_alerted_after_high_time = pump.high_time
                    alerts.append(alert)
                    changed.append(pump)
                    long_exit_triggered = True
            if candle.interval == self.confirm_interval:
                alert = self._short_signal_v2(pump, candle)
                if alert:
                    pump.short_alerted_after_high_time = pump.high_time
                    alerts.append(alert)
                    changed.append(pump)
                    long_exit_triggered = True
            if self.long_enabled and not long_exit_triggered:
                alerts.extend(self._process_long(candle))
            return changed, alerts

        long_exit_triggered = False
        if candle.interval == self.early_interval:
            alert = self._early_alert(pump, candle)
            if alert:
                pump.early_alerted_after_high_time = pump.high_time
                alerts.append(alert)
                changed.append(pump)
                long_exit_triggered = True
        if candle.interval == self.confirm_interval:
            alert = self._short_signal(pump, candle)
            if alert:
                pump.short_alerted_after_high_time = pump.high_time
                alerts.append(alert)
                changed.append(pump)
                long_exit_triggered = True
        if self.long_enabled and not long_exit_triggered:
            alerts.extend(self._process_long(candle))
        return changed, alerts

    def _append_candle(self, candle: Candle) -> bool:
        key = (candle.symbol, candle.interval)
        buffer = self.buffers[key]
        seen = self.buffer_times[key]
        if candle.open_time in seen:
            return False
        if len(buffer) == buffer.maxlen:
            old = buffer.popleft()
            seen.discard(old.open_time)
        buffer.append(candle)
        seen.add(candle.open_time)
        return True

    def _early_alert(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        if pump.early_alerted_after_high_time == pump.high_time:
            return None
        candles = list(self.buffers[(pump.symbol, self.early_interval)])
        lookback = int(self.params.consolidation_lookback)
        if len(candles) < max(int(self.signal_cfg["volume_window"]) + 1, lookback + 1):
            return None
        vol_ratio = current_volume_ratio(candles, int(self.signal_cfg["volume_window"]))
        consolidation = top_consolidation_rejection(candles, pump, self.params)
        remaining = remaining_downside_pct(pump.anchor_price, candle.close)
        if (
            consolidation["ok"]
            and vol_ratio >= self.params.early_volume_ratio
            and remaining >= self.params.early_min_remaining_pct
        ):
            return make_alert(
                "early_alert",
                pump,
                candle,
                vol_ratio,
                remaining,
                [
                    f"{self.early_interval} upside rejection",
                    f"pattern={consolidation.get('pattern', 'unknown')}",
                    f"upper_wick={consolidation['upper_wick_pct']:.2f}%",
                    f"probe={consolidation['probe_pct']:.2f}%",
                    f"range={consolidation['range_pct']:.2f}%",
                    f"drift={consolidation['drift_pct']:+.2f}%",
                ],
            )
        return None

    def _short_signal(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        if pump.short_alerted_after_high_time == pump.high_time:
            return None
        candles = list(self.buffers[(pump.symbol, self.confirm_interval)])
        if len(candles) < int(self.signal_cfg["volume_window"]) + 2:
            return None
        drop = pct_change(candle.close, pump.high_price)
        vol_ratio = current_volume_ratio(candles, int(self.signal_cfg["volume_window"]))
        closes = [c.close for c in candles]
        e21 = ema(closes, int(self.signal_cfg["ema_confirm"]))[-1]
        breaks_structure = breaks_post_high_structure(candles, pump.high_time, int(self.signal_cfg["structure_lookback_5m"])) or candle.close < e21
        remaining = remaining_downside_pct(pump.anchor_price, candle.close)
        if (
            drop >= self.params.confirm_drop_from_high_pct
            and vol_ratio >= self.params.confirm_volume_ratio
            and breaks_structure
            and remaining >= self.params.confirm_min_remaining_pct
        ):
            return make_alert("short_signal", pump, candle, vol_ratio, remaining, [f"{self.confirm_interval} closed breakdown"])
        return None

    # ---- v2 数据驱动信号 ----
    def _early_alert_v2(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        """顶部预警:近妖币高点 + 放量 + 冲高回落(收盘在K线下半部)。多而贴顶,容忍逆向。"""
        if pump.early_alerted_after_high_time == pump.high_time:
            return None
        candles = list(self.buffers[(pump.symbol, self.early_interval)])
        if len(candles) < int(self.signal_cfg["volume_window"]) + 1:
            return None
        p = self.params
        near_high = candle.high >= pump.high_price * (1.0 - p.early_v2_near_high_pct / 100.0)
        vol_ratio = current_volume_ratio(candles, int(self.signal_cfg["volume_window"]))
        cpos = close_position(candle)
        remaining = remaining_downside_pct(pump.anchor_price, candle.close)
        if (
            near_high
            and vol_ratio >= p.early_v2_vol_ratio
            and cpos <= p.early_v2_close_pos_max
            and remaining >= p.early_v2_min_remaining_pct
        ):
            return make_alert(
                "early_alert", pump, candle, vol_ratio, remaining,
                [f"{self.early_interval} climax rejection", "near_high", f"close_pos={cpos:.2f}", f"vol={vol_ratio:.2f}x"],
            )
        return None

    def _short_signal_v2(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        """下跌启动:跌破高位区 + 弱收阴线(+可选主动卖盘)。低逆向、会一直跌。"""
        if pump.short_alerted_after_high_time == pump.high_time:
            return None
        candles = list(self.buffers[(pump.symbol, self.confirm_interval)])
        if len(candles) < int(self.signal_cfg["volume_window"]) + 1:
            return None
        p = self.params
        broke = candle.close < pump.high_price * (1.0 - p.short_v2_break_pct / 100.0)
        red = candle.close < candle.open
        cpos = close_position(candle)
        vol_ratio = current_volume_ratio(candles, int(self.signal_cfg["volume_window"]))
        tsell = taker_sell_ratio(candle)
        remaining = remaining_downside_pct(pump.anchor_price, candle.close)
        if (
            broke
            and red
            and cpos <= p.short_v2_close_pos_max
            and vol_ratio >= p.short_v2_vol_ratio
            and tsell >= p.short_v2_taker_min
            and remaining >= p.short_v2_min_remaining_pct
        ):
            return make_alert(
                "short_signal", pump, candle, vol_ratio, remaining,
                [f"{self.confirm_interval} high-zone breakdown", f"close_pos={cpos:.2f}", f"taker_sell={tsell:.2f}",
                 f"drop_from_high={pct_change(candle.close, pump.high_price):+.2f}%"],
            )
        return None

    def _ml_signals(self, pump: PumpEvent, candle: Candle) -> list[Alert]:
        """ML 模式: 在 setup 候选上用模型打分, 超阈值才发。见顶->early, 破位->short。"""
        if candle.interval != self.confirm_interval or self.ml is None or not self.ml.ready:
            return []
        from ..ml import features as mlf
        candles = list(self.buffers[(pump.symbol, self.confirm_interval)])
        if len(candles) < mlf.LOOKBACK + 2:
            return []
        row = mlf.compute_features(mlf.candles_to_frame(candles)).iloc[-1]
        if row[self.ml.cols].isna().any():
            return []
        out: list[Alert] = []
        vol_ratio = current_volume_ratio(candles, int(self.signal_cfg["volume_window"]))
        remaining = remaining_downside_pct(pump.anchor_price, candle.close)
        for task, level, setup_fn, tag in (
            ("top", "early_alert", mlf.top_setup_flags, "ML见顶分"),
            ("dump", "short_signal", mlf.dump_setup_flags, "ML破位分"),
        ):
            already = pump.early_alerted_after_high_time if level == "early_alert" else pump.short_alerted_after_high_time
            if already == pump.high_time or not bool(setup_fn(row)):
                continue
            sc = self.ml.score(row, task)
            thr = self.ml.threshold(task)
            if sc is None or thr is None or sc < thr:
                continue
            hi = self.ml.threshold_high(task) or 2.0
            tier = "高置信" if sc >= hi else "普通"
            out.append(make_alert(level, pump, candle, vol_ratio, remaining, [f"{tag}={sc:.2f}", f"置信={tier}"]))
        return out


def close_position(candle: Candle) -> float:
    rng = candle.high - candle.low
    return (candle.close - candle.low) / rng if rng > 0 else 0.5


def taker_sell_ratio(candle: Candle) -> float:
    return 1.0 - candle.taker_buy_quote / candle.quote_volume if candle.quote_volume > 0 else 0.0


def infer_anchor_price(record: LiquidityRecord) -> float:
    pct = max(record.pct_15m, record.pct_30m, record.pct_4h, record.pct_12h, record.pct_1d, 0.0)
    return record.last_price / (1.0 + pct / 100.0) if pct > 0 else record.last_price


def strongest_pump_window(record: LiquidityRecord) -> tuple[float, str]:
    windows = [
        (record.pct_15m, "15m"),
        (record.pct_30m, "30m"),
        (record.pct_4h, "4h"),
        (record.pct_12h, "12h"),
        (record.pct_1d, "1d"),
    ]
    pct, label = max(windows, key=lambda item: item[0])
    return max(pct, 0.0), label


def breaks_post_high_structure(candles: list[Candle], high_time: int, lookback: int) -> bool:
    if len(candles) < 2:
        return False
    current = candles[-1]
    prior_after_high = [c for c in candles[:-1] if c.open_time > high_time]
    prior = prior_after_high[-lookback:] if prior_after_high else candles[-lookback - 1 : -1]
    return bool(prior) and current.close < min(body_low(c) for c in prior)


def body_low(candle: Candle) -> float:
    return min(candle.open, candle.close)


def body_high(candle: Candle) -> float:
    return max(candle.open, candle.close)


def top_consolidation_rejection(candles: list[Candle], pump: PumpEvent, params: SignalParams) -> dict[str, float | bool]:
    current = candles[-1]
    lookback = int(params.consolidation_lookback)
    prior = candles[-lookback - 1 : -1]
    if len(prior) < lookback:
        return {"ok": False}

    single = single_bar_rejection(current, prior, pump, params)
    if single["ok"]:
        single["pattern"] = "single_upper_wick"
        return single

    if len(candles) >= lookback + 2:
        two_bar_prior = candles[-lookback - 2 : -2]
        two_bar = two_bar_rejection(candles[-2], current, two_bar_prior, pump, params)
        if two_bar["ok"]:
            two_bar["pattern"] = "two_bar_reversal"
            return two_bar

    return single


def consolidation_stats(prior: list[Candle]) -> dict[str, float]:
    body_lows = [body_low(c) for c in prior]
    body_highs = [body_high(c) for c in prior]
    range_low = min(body_lows)
    range_high = max(body_highs)
    return {
        "range_low": range_low,
        "range_high": range_high,
        "range_pct": pct_change(range_low, range_high),
        "drift_pct": pct_change(prior[0].close, prior[-1].close),
    }


def single_bar_rejection(current: Candle, prior: list[Candle], pump: PumpEvent, params: SignalParams) -> dict[str, float | bool]:
    stats = consolidation_stats(prior)
    upper_wick_pct = safe_div(current.high - body_high(current), current.close, 0.0) * 100.0
    lower_wick = body_low(current) - current.low
    upper_wick = current.high - body_high(current)
    probe_pct = pct_change(stats["range_high"], current.high)
    close_back_inside = current.close <= stats["range_high"] * (1.0 + params.close_back_inside_buffer_pct / 100.0)
    still_high_level = remaining_downside_pct(pump.anchor_price, current.close) >= params.early_min_remaining_pct
    ok = (
        stats["range_pct"] <= params.consolidation_max_range_pct
        and abs(stats["drift_pct"]) <= params.consolidation_max_drift_pct
        and upper_wick_pct >= params.rejection_upper_wick_pct
        and upper_wick > lower_wick
        and probe_pct >= params.rejection_probe_pct
        and close_back_inside
        and still_high_level
    )
    return {
        "ok": ok,
        "range_pct": stats["range_pct"],
        "drift_pct": stats["drift_pct"],
        "upper_wick_pct": upper_wick_pct,
        "probe_pct": probe_pct,
    }


def two_bar_rejection(spike: Candle, confirm: Candle, prior: list[Candle], pump: PumpEvent, params: SignalParams) -> dict[str, float | bool]:
    stats = consolidation_stats(prior)
    probe_pct = pct_change(stats["range_high"], spike.high)
    two_bar_drop_pct = pct_change(spike.close, confirm.close) * -1.0
    close_back_inside = confirm.close <= stats["range_high"] * (1.0 + params.close_back_inside_buffer_pct / 100.0)
    spike_green = spike.close > spike.open
    confirm_red = confirm.close < confirm.open
    still_high_level = remaining_downside_pct(pump.anchor_price, confirm.close) >= params.early_min_remaining_pct
    ok = (
        stats["range_pct"] <= params.consolidation_max_range_pct
        and abs(stats["drift_pct"]) <= params.consolidation_max_drift_pct
        and spike_green
        and confirm_red
        and probe_pct >= params.rejection_probe_pct
        and two_bar_drop_pct >= params.rejection_two_bar_drop_pct
        and close_back_inside
        and still_high_level
    )
    return {
        "ok": ok,
        "range_pct": stats["range_pct"],
        "drift_pct": stats["drift_pct"],
        "upper_wick_pct": 0.0,
        "probe_pct": probe_pct,
        "two_bar_drop_pct": two_bar_drop_pct,
    }


def current_volume_ratio(candles: list[Candle], window: int) -> float:
    if len(candles) <= window:
        return 0.0
    prev = [c.quote_volume for c in candles[-window - 1 : -1]]
    return safe_div(candles[-1].quote_volume, mean(prev), 0.0)


def remaining_downside_pct(anchor_price: float, price: float) -> float:
    return max(0.0, (price - anchor_price) / price * 100.0) if price else 0.0


def make_alert(level: str, pump: PumpEvent, candle: Candle, vol_ratio: float, remaining: float, evidence: list[str]) -> Alert:
    buffer_pct = 1.0 + float(0.01 * 0.35)
    invalidation = max(pump.high_price, candle.high) * buffer_pct
    alert_id = f"{pump.event_id}-{level}-{candle.close_time}"
    pump_gain = (pump.high_price / pump.anchor_price - 1) * 100 if pump.anchor_price > 0 else 0.0
    category = "妖币" if pump_gain >= 50.0 else "普通"
    bottom = BOTTOM_HINT_HOURS.get(level, "18-21")
    occ = _bump_pump_seq(pump, level)
    return Alert(
        alert_id=alert_id,
        event_id=pump.event_id,
        symbol=pump.symbol,
        level=level,
        decision_time=candle.close_time,
        source_candle_close_time=candle.close_time,
        data_cutoff_time=candle.close_time,
        price=candle.close,
        invalidation_price=round(invalidation, 8),
        anchor_price=pump.anchor_price,
        high_price=pump.high_price,
        remaining_downside_pct=round(remaining, 4),
        volume_ratio=round(vol_ratio, 4),
        evidence=[f"类型={category}(涨幅{pump_gain:.0f}%)", f"经验见底≈{bottom}h"] + evidence + [
            f"drop_from_high={pct_change(candle.close, pump.high_price):+.2f}%",
            f"volume_ratio={vol_ratio:.2f}x",
            f"remaining_to_anchor={remaining:.2f}%",
        ],
        risks=[],
        category=category,
        occurrence=occ,
    )


def make_long_alert(le: LongEvent, candle: Candle, trend_break_pct: float, evidence: list[str]) -> Alert:
    """做多信号(纯提示): 启动做多。invalidation=趋势破坏止损位。"""
    le.long_signal_seq += 1
    from_entry = pct_change(le.entry_price, candle.close)
    return Alert(
        alert_id=f"{le.event_id}-long_signal-{candle.close_time}",
        event_id=le.event_id,
        symbol=le.symbol,
        level="long_signal",
        decision_time=candle.close_time,
        source_candle_close_time=candle.close_time,
        data_cutoff_time=candle.close_time,
        price=candle.close,
        invalidation_price=round(le.entry_price * (1.0 - trend_break_pct / 100.0), 8),
        anchor_price=le.entry_price,
        high_price=le.high_price,
        remaining_downside_pct=0.0,
        volume_ratio=0.0,
        evidence=[f"入场≈{le.entry_price:.6g}", f"距入场={from_entry:+.2f}%"] + evidence
        + [f"止损位(趋势破坏-{trend_break_pct:.0f}%)={le.entry_price * (1.0 - trend_break_pct / 100.0):.6g}"],
        risks=[],
        category="做多",
        occurrence=le.long_signal_seq,
    )


def make_long_exit_alert(level: str, le: LongEvent, candle: Candle, category: str, evidence: list[str]) -> Alert:
    """做多持仓遇到顶部/下跌启动时的退出提示。"""
    from_entry = pct_change(le.entry_price, candle.close)
    return Alert(
        alert_id=f"{le.event_id}-{level}-{candle.close_time}",
        event_id=le.event_id,
        symbol=le.symbol,
        level=level,
        decision_time=candle.close_time,
        source_candle_close_time=candle.close_time,
        data_cutoff_time=candle.close_time,
        price=candle.close,
        invalidation_price=round(max(le.high_price, candle.high) * 1.0035, 8),
        anchor_price=le.entry_price,
        high_price=le.high_price,
        remaining_downside_pct=0.0,
        volume_ratio=0.0,
        evidence=[f"做多退出={category}", f"入场≈{le.entry_price:.6g}", f"距入场={from_entry:+.2f}%"] + evidence,
        risks=[],
        category=category,
        occurrence=le.long_signal_seq,
    )


def make_long_status_alert(level: str, le: LongEvent, candle: Candle, reason: str, evidence: list[str]) -> Alert:
    """做多监管结束但不是顶部/做空信号时的状态提示。"""
    from_entry = pct_change(le.entry_price, candle.close)
    return Alert(
        alert_id=f"{le.event_id}-{level}-{candle.close_time}",
        event_id=le.event_id,
        symbol=le.symbol,
        level=level,
        decision_time=candle.close_time,
        source_candle_close_time=candle.close_time,
        data_cutoff_time=candle.close_time,
        price=candle.close,
        invalidation_price=round(le.entry_price, 8),
        anchor_price=le.entry_price,
        high_price=le.high_price,
        remaining_downside_pct=0.0,
        volume_ratio=0.0,
        evidence=[f"做多状态={reason}", f"入场≈{le.entry_price:.6g}", f"距入场={from_entry:+.2f}%"] + evidence,
        risks=[],
        category="做多",
        occurrence=le.long_signal_seq,
    )


# 经验见底用时(小时,来自90天复盘): early/short ~18-21h, fallback ~15h
BOTTOM_HINT_HOURS = {"early_alert": "18-21", "short_signal": "18-21", "fallback_alert": "15"}


def _bump_pump_seq(pump: PumpEvent, level: str) -> int:
    """本次监测周期(同一事件)内该类信号出现第几次。"""
    if level == "early_alert":
        pump.early_alert_seq += 1
        return pump.early_alert_seq
    if level == "short_signal":
        pump.short_signal_seq += 1
        return pump.short_signal_seq
    if level == "fallback_alert":
        pump.fallback_alert_seq += 1
        return pump.fallback_alert_seq
    return 0
