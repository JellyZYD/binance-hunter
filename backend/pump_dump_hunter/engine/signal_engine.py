from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..indicators import ema, mean, pct_change, safe_div
from ..ml import lifecycle as life
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
        self.long_interval = str(self.signal_cfg.get("long_interval", self.confirm_interval))
        self.strategy_version = str(self.signal_cfg.get("strategy_version", "global_ml"))
        self.mode = str(self.signal_cfg.get("mode", "legacy"))
        self.multi_signal_cooldown_hours = float(self.signal_cfg.get("multi_signal_cooldown_hours", 4.0))
        self.multi_signal_cooldown_ms = int(max(0.0, self.multi_signal_cooldown_hours) * 3_600_000)
        self.long_signal_cooldown_hours = float(self.signal_cfg.get("long_signal_cooldown_hours", 2.0))
        self.long_signal_cooldown_ms = int(max(0.0, self.long_signal_cooldown_hours) * 3_600_000)
        self.lifecycle_long_watch_min_gain_pct = float(self.signal_cfg.get("lifecycle_long_watch_min_gain_pct", 15.0))
        self.lifecycle_pump_signal_min_gain_pct = float(self.signal_cfg.get("lifecycle_pump_signal_min_gain_pct", 0.0))
        self.lifecycle_high_pump_enabled = bool(self.signal_cfg.get("lifecycle_high_pump_enabled", False))
        self.lifecycle_high_pump_min_gain_pct = float(self.signal_cfg.get("lifecycle_high_pump_min_gain_pct", 40.0))
        self.lifecycle_min_remaining_pct = float(self.signal_cfg.get("lifecycle_min_remaining_pct", 5.0))
        self.lifecycle_exhaustion_min_gain_pct = float(self.signal_cfg.get("lifecycle_exhaustion_min_gain_pct", 8.0))
        self.lifecycle_route_confirm_bars = int(self.signal_cfg.get("lifecycle_route_confirm_bars", 2))
        self.lifecycle_route_margin = float(self.signal_cfg.get("lifecycle_route_margin", life.DEFAULT_ROUTE_MARGIN))
        self.lifecycle_dynamic_route_thresholds = bool(self.signal_cfg.get("lifecycle_dynamic_route_thresholds", True))
        self.emit_distribution_warning_alerts = bool(self.signal_cfg.get("emit_distribution_warning_alerts", False))
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
        self.buffers: dict[tuple[str, str], deque[Candle]] = defaultdict(lambda: deque(maxlen=640))
        self.buffer_times: dict[tuple[str, str], set[int]] = defaultdict(set)

    def _use_lifecycle(self) -> bool:
        return (
            self.mode == "ml"
            and self.strategy_version in {"lifecycle_expert", "lifecycle_router_expert"}
            and self.ml is not None
            and self.ml.lifecycle_ready
        )

    def _route_thresholds(self, row: Any | None = None) -> dict[str, float]:
        thresholds = {
            "fast_dump": float(self.signal_cfg.get("lifecycle_route_fast_threshold", life.DEFAULT_ROUTE_THRESHOLDS["fast_dump"])),
            "slow_distribution": float(self.signal_cfg.get("lifecycle_route_slow_threshold", life.DEFAULT_ROUTE_THRESHOLDS["slow_distribution"])),
            "second_distribution": float(self.signal_cfg.get("lifecycle_route_second_threshold", life.DEFAULT_ROUTE_THRESHOLDS["second_distribution"])),
            "continuation": float(self.signal_cfg.get("lifecycle_route_continuation_threshold", life.DEFAULT_ROUTE_THRESHOLDS["continuation"])),
        }
        if not self.lifecycle_dynamic_route_thresholds or row is None:
            return thresholds
        get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k])
        state = str(get("behavior_state", "neutral_watch") or "neutral_watch")
        high = max(float(get("ctx_high_since_entry", 0.0) or 0.0), 0.0)
        drawdown = max(-float(get("ctx_drawdown_from_entry_high", 0.0) or 0.0), 0.0)
        hours = max(float(get("ctx_hours_since_entry", 0.0) or 0.0), 0.0)
        if state in {"acceleration", "trend_hold"}:
            thresholds["fast_dump"] = max(thresholds["fast_dump"], float(self.signal_cfg.get("lifecycle_route_fast_trend_threshold", 0.97)))
            thresholds["slow_distribution"] = max(thresholds["slow_distribution"], float(self.signal_cfg.get("lifecycle_route_slow_trend_threshold", 0.82)))
        if state in {"pullback_risk", "breakdown"} and high >= 0.14:
            thresholds["fast_dump"] = min(thresholds["fast_dump"], float(self.signal_cfg.get("lifecycle_route_fast_break_threshold", thresholds["fast_dump"])))
        if state == "breakdown" and high >= 0.16 and drawdown >= 0.08:
            thresholds["slow_distribution"] = min(thresholds["slow_distribution"], float(self.signal_cfg.get("lifecycle_route_slow_break_threshold", thresholds["slow_distribution"])))
        elif state == "distribution" and high >= 0.18 and hours >= 12:
            thresholds["slow_distribution"] = min(thresholds["slow_distribution"], float(self.signal_cfg.get("lifecycle_route_slow_mature_threshold", thresholds["slow_distribution"])))
        return thresholds

    def load_events(self, events: list[PumpEvent]) -> None:
        for event in events:
            self.events_by_symbol[event.symbol] = event

    def load_long_events(self, events: list[LongEvent]) -> None:
        for event in events:
            self.long_events_by_symbol[event.symbol] = event

    def _pump_signal_last_time(self, pump: PumpEvent, level: str) -> int | None:
        if level == "early_alert":
            return pump.early_last_alert_time or (
                pump.early_alerted_after_high_time if pump.early_alerted_after_high_time == pump.high_time else None
            )
        if level == "short_signal":
            return pump.short_last_alert_time or (
                pump.short_alerted_after_high_time if pump.short_alerted_after_high_time == pump.high_time else None
            )
        if level == "fallback_alert":
            return pump.fallback_last_alert_time or (
                pump.fallback_alerted_after_high_time if pump.fallback_alerted_after_high_time == pump.high_time else None
            )
        return None

    def _can_emit_pump_signal(self, pump: PumpEvent, level: str, decision_time: int) -> bool:
        if level == "early_alert" and pump.early_alerted_after_high_time == pump.high_time:
            return False
        if level == "short_signal" and pump.short_alerted_after_high_time == pump.high_time:
            return False
        last_time = self._pump_signal_last_time(pump, level)
        if last_time is None or self.multi_signal_cooldown_ms <= 0:
            return True
        return decision_time - int(last_time) >= self.multi_signal_cooldown_ms

    def _mark_pump_signal(self, pump: PumpEvent, level: str, decision_time: int) -> None:
        if level == "early_alert":
            pump.early_alerted_after_high_time = pump.high_time
            pump.early_last_alert_time = decision_time
        elif level == "short_signal":
            pump.short_alerted_after_high_time = pump.high_time
            pump.short_last_alert_time = decision_time
        elif level == "fallback_alert":
            pump.fallback_alerted_after_high_time = pump.high_time
            pump.fallback_last_alert_time = decision_time

    def _can_emit_long_signal(self, le: LongEvent, decision_time: int) -> bool:
        if le.long_last_signal_time is None or self.long_signal_cooldown_ms <= 0:
            return True
        return decision_time - int(le.long_last_signal_time) >= self.long_signal_cooldown_ms

    def _long_blocked_by_pump_risk(self, symbol: str, decision_time: int) -> bool:
        pump = self.events_by_symbol.get(symbol)
        if pump is None or pump.status != "active" or pump.expires_at < decision_time:
            return False
        return bool(pump.early_last_alert_time or pump.short_last_alert_time)

    def on_discovery(self, records: list[LiquidityRecord], decision_time: int) -> list[PumpEvent]:
        changed = []
        active_ms = int(self.active_hours * 3_600_000)
        for record in records:
            if self.long_enabled:
                le = self.long_events_by_symbol.get(record.symbol)
                if le and le.status == "active" and record.selected and not record.long_candidate:
                    le.status = "closed"
                    le.exit_reason = "long_candidate_lost"
                if record.long_candidate and self._long_blocked_by_pump_risk(record.symbol, decision_time):
                    le = self.long_events_by_symbol.get(record.symbol)
                    if le and le.status == "active":
                        le.status = "closed"
                        le.exit_reason = "pump_risk_conflict"
                    continue
                if record.long_candidate:
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
            self._copy_long_rank_fields(le, record)
        else:
            self.long_events_by_symbol[record.symbol] = LongEvent(
                event_id=f"{record.symbol}-L-{decision_time}",
                symbol=record.symbol, first_seen=decision_time, last_seen=decision_time,
                expires_at=decision_time + watch_ms, entry_price=record.last_price,
                high_price=record.last_price, current_price=record.last_price,
                qv30_rank=record.qvol30_rank,
                ret30_rank=record.ret30_rank,
                qv30_rank_pct=record.qvol30_rank_pct,
                ret30_rank_pct=record.ret30_rank_pct,
                evidence=[f"入选做多: 30m={record.pct_30m:+.2f}% 量比={record.volume_ratio_30m:.1f}x 4h={record.pct_4h:+.2f}%"],
            )

    @staticmethod
    def _copy_long_rank_fields(le: LongEvent, record: LiquidityRecord) -> None:
        le.qv30_rank = record.qvol30_rank
        le.ret30_rank = record.ret30_rank
        le.qv30_rank_pct = record.qvol30_rank_pct
        le.ret30_rank_pct = record.ret30_rank_pct

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
        if self._long_blocked_by_pump_risk(le.symbol, candle.close_time):
            le.status = "closed"
            le.exit_reason = "pump_risk_conflict"
            return []
        if candle.close_time > le.expires_at:
            le.status = "closed"; le.exit_reason = "超时"
            return [make_long_status_alert("long_timeout", le, candle, "监管超时", [f"watch_hours={self.long_watch_hours:.0f}"])]
        le.current_price = candle.close
        le.high_price = max(le.high_price, candle.high)
        le.last_seen = candle.close_time
        if candle.close <= le.entry_price * (1.0 - self.long_trend_break_pct / 100.0):
            le.status = "closed"; le.exit_reason = "趋势破坏"
            return []
        exit_alerts = self._long_exit_signals(le, candle)
        if exit_alerts:
            return exit_alerts
        return self._long_signals(le, candle)

    def _long_exit_signals(self, le: LongEvent, candle: Candle) -> list[Alert]:
        if self._use_lifecycle():
            return []
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
        if self._use_lifecycle():
            return self._lifecycle_long_signals(le, candle)
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
        if not self._can_emit_long_signal(le, candle.close_time):
            return []
        hi = self.ml.threshold_high("long") or 2.0
        tier = "高置信" if sc >= hi else "普通观察"
        le.long_last_signal_time = candle.close_time
        return [make_long_alert(le, candle, self.long_trend_break_pct, [f"ML做多分={sc:.2f}", f"置信={tier}"])]

    def _lifecycle_long_signals(self, le: LongEvent, candle: Candle) -> list[Alert]:
        if candle.interval != self.long_interval or self.ml is None or not self.ml.lifecycle_ready:
            return []
        interval_ms = interval_to_ms(self.long_interval)
        candles = sorted(self.buffers[(le.symbol, self.long_interval)], key=lambda c: c.open_time)
        if len(candles) < life.bars_15m_units(life.LOOKBACK_UNITS, interval_ms) + 2:
            return []
        frame = life.candles_to_frame(candles)
        features = life.compute_features(frame, interval_ms, raw_lag_mode="native")
        rank_values = {
            "qv30_rank": le.qv30_rank,
            "ret30_rank": le.ret30_rank,
            "qv30_rank_pct": le.qv30_rank_pct,
            "ret30_rank_pct": le.ret30_rank_pct,
        }
        row = life.add_long_extras(frame, features, rank_values, interval_ms)
        cols = self.ml.lifecycle_columns("long_pump_event")
        if not cols or not life.finite_for(row, cols):
            return []
        scores = self.ml.lifecycle_long_score(row)
        score = scores.get("score")
        thr = self.ml.lifecycle_long_threshold(high=False)
        if score is None or thr is None or score < thr:
            return []
        if not self._can_emit_long_signal(le, candle.close_time):
            return []
        high_thr = self.ml.lifecycle_long_threshold(high=True)
        tier = "high" if high_thr is not None and score >= high_thr else "normal"
        evidence = [
            f"strategy={self.strategy_version}",
            f"interval={self.long_interval}",
            "model=lifecycle_long_combo",
            f"score={score:.3f}",
            f"threshold={thr:.3f}",
            f"tier={tier}",
            f"pump_score={float(scores.get('pump') or 0.0):.3f}",
            f"quality_score={float(scores.get('quality') or 0.0):.3f}",
            f"qv30_rank={le.qv30_rank}",
            f"ret30_rank={le.ret30_rank}",
            f"long_cooldown={self.long_signal_cooldown_hours:g}h",
        ]
        le.long_last_signal_time = candle.close_time
        self._ensure_pump_watch_from_long(le, candle, evidence)
        return [
            make_long_alert(
                le,
                candle,
                self.long_trend_break_pct,
                evidence,
                lifecycle_mode="long_entry",
                behavior_state="entry_watch",
                model_name="lifecycle_long_combo",
                model_score=float(score),
                model_threshold=float(thr),
                signal_interval=self.long_interval,
            )
        ]

    def _ensure_pump_watch_from_long(self, le: LongEvent, candle: Candle, evidence: list[str]) -> PumpEvent:
        active_ms = int(self.active_hours * 3_600_000)
        existing = self.events_by_symbol.get(le.symbol)
        high_price = max(le.high_price, candle.high)
        if existing and existing.status == "active" and existing.expires_at >= candle.close_time:
            existing.last_seen = candle.close_time
            existing.expires_at = max(existing.expires_at, candle.close_time + active_ms)
            existing.current_price = candle.close
            if high_price > existing.high_price * (1.0 + self.params.new_high_reset_pct / 100.0):
                existing.high_price = high_price
                existing.high_time = candle.close_time
                existing.early_alerted_after_high_time = None
                existing.short_alerted_after_high_time = None
                existing.fallback_alerted_after_high_time = None
            existing.max_gain_pct = pct_change(existing.anchor_price, existing.high_price)
            existing.evidence = sorted(set(existing.evidence + ["source=long_signal_pump_watch"] + evidence))
            return existing
        pump = PumpEvent(
            event_id=f"{le.symbol}-PW-{le.first_seen}",
            symbol=le.symbol,
            first_seen=le.first_seen,
            last_seen=candle.close_time,
            expires_at=candle.close_time + active_ms,
            trigger_window=f"long_{self.long_interval}",
            anchor_price=le.entry_price,
            high_price=high_price,
            high_time=candle.close_time,
            current_price=candle.close,
            max_gain_pct=pct_change(le.entry_price, high_price),
            lifecycle_mode="long_entry",
            behavior_state="entry_watch",
            lifecycle_updated_time=candle.close_time,
            evidence=["source=long_signal_pump_watch"] + evidence,
        )
        self.events_by_symbol[le.symbol] = pump
        return pump

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
                pump_after_long = self.events_by_symbol.get(candle.symbol)
                if pump_after_long and pump_after_long.status == "active" and pump_after_long.expires_at >= candle.close_time:
                    changed.append(pump_after_long)
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
                self._mark_pump_signal(pump, alert.level, alert.decision_time)
                if alert.level == "early_alert":
                    le = self.long_events_by_symbol.get(candle.symbol)
                    if le and le.status == "active":  # 见顶 = 平多, 结束做多监管
                        le.status = "closed"; le.exit_reason = "见顶"
                        long_exit_triggered = True
                elif alert.level == "short_signal":
                    le = self.long_events_by_symbol.get(candle.symbol)
                    if le and le.status == "active":  # 下跌启动 = 平多/做空, 结束做多监管
                        le.status = "closed"; le.exit_reason = "下跌启动"
                        long_exit_triggered = True
                alerts.append(alert)
                changed.append(pump)
            if self._use_lifecycle() and candle.interval == self.confirm_interval and pump not in changed:
                changed.append(pump)
            if self.long_enabled and not long_exit_triggered:
                alerts.extend(self._process_long(candle))
            return changed, alerts

        if self.mode == "v2":
            long_exit_triggered = False
            if candle.interval == self.early_interval:
                alert = self._early_alert_v2(pump, candle)
                if alert:
                    self._mark_pump_signal(pump, alert.level, alert.decision_time)
                    alerts.append(alert)
                    changed.append(pump)
                    long_exit_triggered = True
            if candle.interval == self.confirm_interval:
                alert = self._short_signal_v2(pump, candle)
                if alert:
                    self._mark_pump_signal(pump, alert.level, alert.decision_time)
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
                self._mark_pump_signal(pump, alert.level, alert.decision_time)
                alerts.append(alert)
                changed.append(pump)
                long_exit_triggered = True
        if candle.interval == self.confirm_interval:
            alert = self._short_signal(pump, candle)
            if alert:
                self._mark_pump_signal(pump, alert.level, alert.decision_time)
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
        if not self._can_emit_pump_signal(pump, "early_alert", candle.close_time):
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
                    f"cooldown={self.multi_signal_cooldown_hours:g}h",
                ],
            )
        return None

    def _short_signal(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        if not self._can_emit_pump_signal(pump, "short_signal", candle.close_time):
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
            return make_alert("short_signal", pump, candle, vol_ratio, remaining, [
                f"{self.confirm_interval} closed breakdown",
                f"cooldown={self.multi_signal_cooldown_hours:g}h",
            ])
        return None

    # ---- v2 数据驱动信号 ----
    def _early_alert_v2(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        """顶部预警:近妖币高点 + 放量 + 冲高回落(收盘在K线下半部)。多而贴顶,容忍逆向。"""
        if not self._can_emit_pump_signal(pump, "early_alert", candle.close_time):
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
                [
                    f"{self.early_interval} climax rejection",
                    "near_high",
                    f"close_pos={cpos:.2f}",
                    f"vol={vol_ratio:.2f}x",
                    f"cooldown={self.multi_signal_cooldown_hours:g}h",
                ],
            )
        return None

    def _short_signal_v2(self, pump: PumpEvent, candle: Candle) -> Alert | None:
        """下跌启动:跌破高位区 + 弱收阴线(+可选主动卖盘)。低逆向、会一直跌。"""
        if not self._can_emit_pump_signal(pump, "short_signal", candle.close_time):
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
                 f"drop_from_high={pct_change(candle.close, pump.high_price):+.2f}%",
                 f"cooldown={self.multi_signal_cooldown_hours:g}h"],
            )
        return None

    def _ml_signals(self, pump: PumpEvent, candle: Candle) -> list[Alert]:
        if self._use_lifecycle():
            return self._lifecycle_signals(pump, candle)
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
            if not self._can_emit_pump_signal(pump, level, candle.close_time) or not bool(setup_fn(row)):
                continue
            sc = self.ml.score(row, task)
            thr = self.ml.threshold(task)
            if sc is None or thr is None or sc < thr:
                continue
            hi = self.ml.threshold_high(task) or 2.0
            tier = "高置信" if sc >= hi else "普通"
            out.append(make_alert(
                level,
                pump,
                candle,
                vol_ratio,
                remaining,
                [f"{tag}={sc:.2f}", f"置信={tier}", f"cooldown={self.multi_signal_cooldown_hours:g}h"],
            ))
        return out

    def _lifecycle_signals(self, pump: PumpEvent, candle: Candle) -> list[Alert]:
        if candle.interval != self.confirm_interval or self.ml is None or not self.ml.lifecycle_ready:
            return []
        interval_ms = interval_to_ms(self.confirm_interval)
        candles = sorted(self.buffers[(pump.symbol, self.confirm_interval)], key=lambda c: c.open_time)
        row = life.build_lifecycle_row(candles, pump.first_seen, pump.anchor_price, interval_ms)
        if row is None:
            return []
        state = str(row.get("behavior_state", "neutral_watch") or "neutral_watch")
        vol_ratio = current_volume_ratio(candles, int(self.signal_cfg["volume_window"]))
        remaining = remaining_downside_pct(pump.anchor_price, candle.close)
        pump.behavior_state = state
        pump.lifecycle_updated_time = candle.close_time
        route = self._update_lifecycle_route(pump, row, candle.close_time)
        if self._lifecycle_pump_exhausted(pump, row, remaining):
            pump.status = "closed"
            pump.lifecycle_mode = "completed"
            pump.evidence = sorted(set(pump.evidence + [
                f"completed=remaining_to_anchor<{self.lifecycle_min_remaining_pct:g}%",
                f"completed_at={candle.close_time}",
            ]))
            return []
        ready, ready_reason = self._lifecycle_pump_signal_ready(pump, row)
        if not ready:
            pump.lifecycle_mode = "long_watch" if is_long_derived_pump(pump) else "shadow_watch"
            if ready_reason:
                prefix = ready_reason.split("=", 1)[0] + "="
                pump.evidence = sorted(set([e for e in pump.evidence if not e.startswith(prefix)] + [ready_reason]))
            return []

        high_pump_alerts = self._high_pump_lifecycle_signals(
            pump,
            candle,
            candles,
            interval_ms,
            vol_ratio,
            remaining,
        )
        if high_pump_alerts:
            return high_pump_alerts

        route_mode = pump.route_mode or "unknown"
        if route_mode in {"unknown", "continuation", "second_distribution"}:
            pump.lifecycle_mode = f"{route_mode}_watch"
            return []

        model_plan: list[tuple[str, str, str, set[str]]] = []
        if route_mode == "fast_dump":
            model_plan = [
                ("fast_top", "early_alert", "fast_dump", life.FAST_TOP_GATE),
                ("fast_short", "short_signal", "fast_dump", life.FAST_SHORT_GATE),
            ]
        elif route_mode == "slow_distribution":
            model_plan = [
                ("slow_warning", "distribution_warning", "slow_distribution", life.SLOW_TOP_GATE),
                ("slow_short", "short_signal", "slow_distribution", life.SLOW_SHORT_GATE),
            ]
        scored: list[dict[str, Any]] = []
        for model_name, level, mode_name, gate in model_plan:
            cols = self.ml.lifecycle_columns(model_name)
            if not cols or not life.finite_for(row, cols):
                continue
            score = self.ml.lifecycle_score(row, model_name)
            threshold = self.ml.lifecycle_threshold(model_name)
            if score is None or threshold is None or threshold <= 0:
                continue
            scored.append(
                {
                    "model": model_name,
                    "level": level,
                    "mode": mode_name,
                    "gate": gate,
                    "score": float(score),
                    "threshold": float(threshold),
                    "ratio": float(score) / float(threshold),
                }
            )
        pump.lifecycle_mode = route_mode if scored else f"{route_mode}_watch"

        short_ready = [
            item
            for item in scored
            if item["level"] == "short_signal"
            and state in item["gate"]
            and item["score"] >= item["threshold"]
        ]
        if short_ready:
            if not self._can_emit_pump_signal(pump, "short_signal", candle.close_time):
                return []
            return [
                self._make_lifecycle_alert(
                    "short_signal",
                    pump,
                    candle,
                    vol_ratio,
                    remaining,
                    state,
                    max(short_ready, key=lambda x: x["ratio"]),
                )
            ]

        warning_ready = [
            item
            for item in scored
            if item["level"] == "distribution_warning"
            and state in item["gate"]
            and item["score"] >= item["threshold"]
        ]
        if warning_ready:
            best_warning = max(warning_ready, key=lambda x: x["ratio"])
            pump.lifecycle_mode = "distribution_warning"
            pump.evidence = sorted(set(pump.evidence + [
                "route=slow_distribution",
                f"distribution_warning_score={best_warning['score']:.3f}",
                f"distribution_warning_state={state}",
            ]))
            if not self.emit_distribution_warning_alerts:
                return []
            return [
                self._make_lifecycle_alert(
                    "distribution_warning",
                    pump,
                    candle,
                    vol_ratio,
                    remaining,
                    state,
                    best_warning,
                )
            ]

        early_ready = [
            item
            for item in scored
            if item["level"] == "early_alert"
            and state in item["gate"]
            and item["score"] >= item["threshold"]
        ]
        if not early_ready or not self._can_emit_pump_signal(pump, "early_alert", candle.close_time):
            return []
        return [
            self._make_lifecycle_alert(
                "early_alert",
                pump,
                candle,
                vol_ratio,
                remaining,
                state,
                max(early_ready, key=lambda x: x["ratio"]),
            )
        ]

    def _high_pump_lifecycle_signals(
        self,
        pump: PumpEvent,
        candle: Candle,
        candles: list[Candle],
        interval_ms: int,
        vol_ratio: float,
        remaining: float,
    ) -> list[Alert]:
        if not self.lifecycle_high_pump_enabled or self.ml is None:
            return []
        row = life.build_high_pump_row(
            candles,
            pump.first_seen,
            pump.anchor_price,
            self.lifecycle_high_pump_min_gain_pct,
            interval_ms,
        )
        if row is None:
            return []
        if "high_pump_short_emitted" in pump.evidence:
            return []
        state = str(row.get("behavior_state", "neutral_watch") or "neutral_watch")
        plan = [
            ("high_short", "short_signal", "high_pump_short", life.HIGH_PUMP_SHORT_GATE, life.high_pump_short_setup),
            ("high_top", "early_alert", "high_pump_top", life.HIGH_PUMP_TOP_GATE, life.high_pump_top_setup),
        ]
        ready: list[dict[str, Any]] = []
        for model_name, level, mode_name, gate, setup_fn in plan:
            if level == "early_alert" and "high_pump_top_emitted" in pump.evidence:
                continue
            if level == "short_signal" and "high_pump_short_emitted" in pump.evidence:
                continue
            if state not in gate or not setup_fn(row):
                continue
            if not self._can_emit_pump_signal(pump, level, candle.close_time):
                continue
            cols = self.ml.lifecycle_columns(model_name)
            if not cols or not life.finite_for(row, cols):
                continue
            score = self.ml.lifecycle_score(row, model_name)
            threshold = self.ml.lifecycle_threshold(model_name)
            if score is None or threshold is None or threshold <= 0 or score < threshold:
                continue
            ready.append(
                {
                    "model": model_name,
                    "level": level,
                    "mode": mode_name,
                    "gate": gate,
                    "score": float(score),
                    "threshold": float(threshold),
                    "ratio": float(score) / float(threshold),
                }
            )
        if not ready:
            return []
        best = max(ready, key=lambda x: (x["level"] == "short_signal", x["ratio"]))
        alert = self._make_lifecycle_alert(
            best["level"],
            pump,
            candle,
            vol_ratio,
            remaining,
            state,
            best,
        )
        alert.evidence = sorted(
            set(
                alert.evidence
                + [
                    f"high_pump_min_gain={self.lifecycle_high_pump_min_gain_pct:g}%",
                    "route_policy=high_pump_before_family_router",
                ]
            )
        )
        flag = "high_pump_short_emitted" if best["level"] == "short_signal" else "high_pump_top_emitted"
        pump.evidence = sorted(set(pump.evidence + [flag]))
        return [alert]

    def _update_lifecycle_route(self, pump: PumpEvent, row: Any, decision_time: int) -> dict[str, Any]:
        if self.ml is None or not getattr(self.ml, "lifecycle_router_ready", False):
            pump.route_mode = "unknown"
            pump.route_candidate = "unknown"
            pump.route_confidence = 0.0
            pump.route_margin = 0.0
            pump.route_probs = {}
            pump.route_updated_time = decision_time
            pump.lifecycle_mode = "router_missing"
            return {"mode": "unknown", "candidate": "unknown", "confidence": 0.0, "margin": 0.0, "probs": {}}
        cols = self.ml.lifecycle_columns("family_router")
        if not cols or not life.finite_for(row, cols):
            pump.route_mode = "unknown"
            pump.route_candidate = "unknown"
            pump.route_confidence = 0.0
            pump.route_margin = 0.0
            pump.route_probs = {}
            pump.route_updated_time = decision_time
            return {"mode": "unknown", "candidate": "unknown", "confidence": 0.0, "margin": 0.0, "probs": {}}
        probs = self.ml.lifecycle_probabilities(row, "family_router")
        if not probs:
            pump.route_mode = "unknown"
            pump.route_candidate = "unknown"
            pump.route_confidence = 0.0
            pump.route_margin = 0.0
            pump.route_probs = {}
            pump.route_updated_time = decision_time
            return {"mode": "unknown", "candidate": "unknown", "confidence": 0.0, "margin": 0.0, "probs": {}}
        route = life.route_from_probabilities(
            probs,
            thresholds=self._route_thresholds(row),
            margin_threshold=self.lifecycle_route_margin,
        )
        candidate = str(route["mode"] or "unknown")
        previous = pump.route_candidate or "unknown"
        if candidate == "unknown":
            streak = 0
            confirmed = "unknown"
        else:
            streak = pump.route_streak + 1 if previous == candidate else 1
            confirmed = candidate if streak >= max(1, self.lifecycle_route_confirm_bars) else "unknown"
        pump.route_candidate = candidate
        pump.route_streak = streak
        pump.route_mode = confirmed
        pump.route_confidence = float(route["confidence"])
        pump.route_margin = float(route["margin"])
        pump.route_probs = dict(route["probs"])
        pump.route_updated_time = decision_time
        if confirmed != "unknown":
            pump.lifecycle_mode = confirmed
        return route

    def _make_lifecycle_alert(
        self,
        level: str,
        pump: PumpEvent,
        candle: Candle,
        vol_ratio: float,
        remaining: float,
        state: str,
        best: dict[str, Any],
    ) -> Alert:
        pump.lifecycle_mode = best["mode"]
        evidence = [
            f"strategy={self.strategy_version}",
            f"interval={self.confirm_interval}",
            f"mode={best['mode']}",
            f"state={state}",
            f"route={pump.route_mode}",
            f"route_confidence={pump.route_confidence:.3f}",
            f"route_margin={pump.route_margin:.3f}",
            f"model={best['model']}",
            f"score={best['score']:.3f}",
            f"threshold={best['threshold']:.3f}",
            f"ratio={best['ratio']:.2f}",
            f"cooldown={self.multi_signal_cooldown_hours:g}h",
        ]
        return make_alert(
            level,
            pump,
            candle,
            vol_ratio,
            remaining,
            evidence,
            category_override=best["mode"],
            lifecycle_mode=best["mode"],
            behavior_state=state,
            model_name=best["model"],
            model_score=best["score"],
            model_threshold=best["threshold"],
            signal_interval=self.confirm_interval,
            route_mode=pump.route_mode,
            route_confidence=pump.route_confidence,
            route_margin=pump.route_margin,
        )

    def _lifecycle_pump_signal_ready(self, pump: PumpEvent, row: Any) -> tuple[bool, str]:
        if not is_long_derived_pump(pump):
            high_gain = self._lifecycle_high_gain_pct(pump, row)
            if high_gain < self.lifecycle_pump_signal_min_gain_pct:
                return (
                    False,
                    f"pump_watch_not_mature=high_gain{high_gain:.2f}%<min{self.lifecycle_pump_signal_min_gain_pct:g}%",
                )
            return True, ""
        high_gain = self._lifecycle_high_gain_pct(pump, row)
        if high_gain < self.lifecycle_long_watch_min_gain_pct:
            return (
                False,
                f"long_watch_not_mature=high_gain{high_gain:.2f}%<min{self.lifecycle_long_watch_min_gain_pct:g}%",
            )
        return True, ""

    def _lifecycle_pump_exhausted(self, pump: PumpEvent, row: Any, remaining: float) -> bool:
        if self.lifecycle_min_remaining_pct <= 0:
            return False
        if remaining >= self.lifecycle_min_remaining_pct:
            return False
        return self._lifecycle_high_gain_pct(pump, row) >= self.lifecycle_exhaustion_min_gain_pct

    @staticmethod
    def _lifecycle_high_gain_pct(pump: PumpEvent, row: Any) -> float:
        get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k])
        try:
            row_high = float(get("ctx_high_since_entry", 0.0) or 0.0) * 100.0
        except Exception:
            row_high = 0.0
        return max(float(pump.max_gain_pct or 0.0), row_high)


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


def is_long_derived_pump(pump: PumpEvent) -> bool:
    return pump.trigger_window.startswith("long_") or "source=long_signal_pump_watch" in pump.evidence


def make_alert(
    level: str,
    pump: PumpEvent,
    candle: Candle,
    vol_ratio: float,
    remaining: float,
    evidence: list[str],
    *,
    category_override: str | None = None,
    lifecycle_mode: str = "",
    behavior_state: str = "",
    model_name: str = "",
    model_score: float = 0.0,
    model_threshold: float = 0.0,
    signal_interval: str = "",
    route_mode: str = "",
    route_confidence: float = 0.0,
    route_margin: float = 0.0,
) -> Alert:
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
        category=category_override or category,
        occurrence=occ,
        lifecycle_mode=lifecycle_mode,
        behavior_state=behavior_state,
        model_name=model_name,
        model_score=model_score,
        model_threshold=model_threshold,
        signal_interval=signal_interval,
        route_mode=route_mode,
        route_confidence=route_confidence,
        route_margin=route_margin,
    )


def make_long_alert(
    le: LongEvent,
    candle: Candle,
    trend_break_pct: float,
    evidence: list[str],
    *,
    lifecycle_mode: str = "",
    behavior_state: str = "",
    model_name: str = "",
    model_score: float = 0.0,
    model_threshold: float = 0.0,
    signal_interval: str = "",
) -> Alert:
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
        lifecycle_mode=lifecycle_mode,
        behavior_state=behavior_state,
        model_name=model_name,
        model_score=model_score,
        model_threshold=model_threshold,
        signal_interval=signal_interval,
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
