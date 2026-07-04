'use client';

import { useEffect, useMemo, useState } from 'react';

type Summary = {
  tables?: Record<string, number>;
  active_pump_events?: number;
  raw_active_pump_events?: number;
  shadow_pump_events?: number;
  latest_snapshot_time_iso?: string | null;
  latest_data_cutoff_time_iso?: string | null;
  latest_alert_time_iso?: string | null;
  strategy?: StrategyMeta;
};

type StrategyMeta = {
  mode?: string;
  strategy_version?: string;
  early_interval?: string;
  confirm_interval?: string;
  long_interval?: string;
  multi_signal_cooldown_hours?: number;
  long_signal_cooldown_hours?: number;
  lifecycle_long_watch_min_gain_pct?: number;
  lifecycle_min_remaining_pct?: number;
  lifecycle_route_confirm_bars?: number;
  lifecycle_route_margin?: number;
  lifecycle_dynamic_route_thresholds?: boolean;
  lifecycle_route_fast_threshold?: number;
  lifecycle_route_slow_threshold?: number;
  lifecycle_route_fast_break_threshold?: number;
  lifecycle_route_slow_break_threshold?: number;
  lifecycle_pump_signal_min_gain_pct?: number;
  lifecycle_formal_watch_min_gain_pct?: number;
  lifecycle_high_pump_enabled?: boolean;
  lifecycle_high_pump_min_gain_pct?: number;
  long_enabled?: boolean;
};

type LiquidityRow = {
  symbol: string;
  rank: number;
  pct_15m: number;
  pct_30m: number;
  pct_4h: number;
  pct_12h: number;
  pct_1d: number;
  quote_volume_15m: number;
  quote_volume_30m: number;
  volume_ratio_15m: number;
  volume_ratio_30m: number;
  pump_qualified: number;
};

type PumpRow = {
  event_id?: string;
  symbol: string;
  trigger_window: string;
  high_price: number;
  current_price: number;
  max_gain_pct: number;
  expires_at: number;
  status?: string;
  last_seen?: number;
  evidence?: string[];
  first_seen?: number;
  anchor_price?: number;
  early_last_alert_time?: number | null;
  short_last_alert_time?: number | null;
  lifecycle_mode?: string;
  behavior_state?: string;
  lifecycle_updated_time?: number | null;
  route_mode?: string;
  route_candidate?: string;
  route_confidence?: number;
  route_margin?: number;
  route_streak?: number;
  route_probs?: Record<string, number>;
  is_formal_watch?: boolean;
  monitor_stage?: string;
  monitor_stage_label?: string;
  formal_watch_min_gain_pct?: number;
  long_derived_watch?: boolean;
};

type Candle = {
  open_time: number;
  open: number;
  high: number;
  low: number;
  close: number;
};

type AlertRow = {
  alert_id: string;
  symbol: string;
  level: string;
  decision_time: number;
  price: number;
  invalidation_price: number;
  remaining_downside_pct: number;
  volume_ratio: number;
  evidence?: string[];
  occurrence?: number;
  category?: string;
  lifecycle_mode?: string;
  behavior_state?: string;
  model_name?: string;
  model_score?: number;
  model_threshold?: number;
  signal_interval?: string;
  route_mode?: string;
  route_confidence?: number;
  route_margin?: number;
};

type BacktestRow = {
  run_id: string;
  created_at: string;
  days: number;
  metrics?: Record<string, number | string | null | undefined>;
};

type DashboardData = {
  summary: Summary;
  liquidity: LiquidityRow[];
  pumps: PumpRow[];
  alerts: AlertRow[];
  backtests: BacktestRow[];
};

type LongRow = {
  event_id?: string;
  symbol: string;
  first_seen?: number;
  entry_price: number;
  high_price: number;
  current_price: number;
  long_signal_seq: number;
  long_last_signal_time?: number | null;
  status?: string;
  exit_reason?: string;
  evidence?: string[];
  expires_at: number;
  last_seen: number;
};

type ModelMeta = {
  ready?: boolean;
  error?: string;
  trained_at?: string;
  data_start?: string;
  data_end?: string;
  days?: number;
  n_symbols?: number;
  dump?: { val_auc?: number };
  top?: { val_auc?: number };
  long?: { val_auc?: number };
  lifecycle?: {
    strategy_version?: string;
    trained_data?: {
      data_start?: string;
      data_end?: string;
      symbols?: number;
    };
    runtime?: {
      long_interval?: string;
      expert_interval?: string;
      multi_signal_cooldown_hours?: number;
      pump_signal_min_gain_pct?: number;
      high_pump_enabled?: boolean;
      high_pump_min_gain_pct?: number;
    };
    long_score?: {
      threshold?: number;
      threshold_high?: number;
    };
    models?: Record<string, { interval?: string; mode?: string; signal_level?: string; threshold?: number }>;
  };
  lifecycle_ready?: boolean;
};

type MonitorRow = PumpRow & {
  latestAlert?: AlertRow;
  history: AlertRow[];
  drawdownPct: number;
};

async function api<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  return res.json();
}

function fmt(value: unknown, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function pctText(value: unknown, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  return `${n.toFixed(digits)}%`;
}

function pct(value: unknown) {
  const n = Number(value);
  if (!Number.isFinite(n)) return <span>-</span>;
  return <span className={n >= 0 ? 'trend-up' : 'trend-down'}>{pctText(n)}</span>;
}

function date(value?: number | string | null) {
  if (!value) return '-';
  const d = typeof value === 'number' ? new Date(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString();
}

function timeOnly(value?: number | string | null) {
  if (!value) return '-';
  const d = typeof value === 'number' ? new Date(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleTimeString();
}

function drawdown(high: number, current: number) {
  if (!Number.isFinite(high) || !Number.isFinite(current) || high <= 0 || current <= 0) return 0;
  return (high / current - 1) * 100;
}

function signalLabel(level?: string) {
  if (level === 'early_alert') return '顶部预警';
  if (level === 'short_signal') return '下跌启动';
  if (level === 'long_signal') return '做多观察';
  if (level === 'long_timeout') return '做多超时';
  if (level === 'fallback_alert') return '回落兜底';
  return level || '等待信号';
}

function lifecycleModeText(mode?: string) {
  if (mode === 'high_pump_top') return 'high-pump 40% top';
  if (mode === 'high_pump_short') return 'high-pump 40% short';
  if (mode === 'fast_dump') return '快拉急跌';
  if (mode === 'slow_distribution') return '高位派发';
  if (mode === 'long_entry') return '做多启动';
  if (mode === 'shadow_watch') return '影子观察';
  if (mode === 'trend_watch') return '趋势观察';
  if (mode === 'risk_watch') return '风险观察';
  return mode || '未分型';
}

function behaviorText(state?: string) {
  if (state === 'acceleration') return '加速';
  if (state === 'trend_hold') return '趋势保持';
  if (state === 'distribution') return '派发';
  if (state === 'climax_risk') return '冲顶风险';
  if (state === 'pullback_risk') return '回落风险';
  if (state === 'breakdown') return '破位';
  if (state === 'entry_watch') return '入场观察';
  if (state === 'neutral_watch') return '中性观察';
  return state || '等待阶段';
}

function routeModeText(mode?: string) {
  if (mode === 'fast_dump') return 'fast 快拉急跌';
  if (mode === 'slow_distribution') return 'slow 高位派发';
  if (mode === 'second_distribution') return 'second 二次高位';
  if (mode === 'continuation') return 'continuation 禁空';
  if (mode === 'unknown') return 'unknown 观察';
  return mode || 'unknown 观察';
}

function routeSummary(row?: Pick<PumpRow, 'route_mode' | 'route_candidate' | 'route_confidence' | 'route_margin' | 'route_streak' | 'route_probs'>) {
  if (!row) return '';
  const probs = row.route_probs || {};
  const fast = Number(probs.fast_dump ?? 0);
  const slow = Number(probs.slow_distribution ?? 0) + Number(probs.second_distribution ?? 0);
  const conf = Number(row.route_confidence ?? 0);
  const margin = Number(row.route_margin ?? 0);
  const streak = Number(row.route_streak ?? 0);
  return [
    routeModeText(row.route_mode),
    `cand ${row.route_candidate || '-'}`,
    `streak ${streak}`,
    `conf ${fmt(conf, 3)}`,
    `m ${fmt(margin, 3)}`,
    `fast ${fmt(fast, 3)}`,
    `slow ${fmt(slow, 3)}`,
  ].join(' / ');
}

function routeCompact(row?: Pick<PumpRow, 'route_mode' | 'route_confidence'>) {
  if (!row?.route_mode || row.route_mode === 'unknown') return '';
  const conf = Number(row.route_confidence ?? 0);
  return `${routeModeText(row.route_mode)} ${fmt(conf, 2)}`;
}

function alertRouteSummary(alert?: AlertRow) {
  if (!alert?.route_mode) return '';
  const conf = Number(alert.route_confidence ?? 0);
  const margin = Number(alert.route_margin ?? 0);
  return `${routeModeText(alert.route_mode)} / conf ${fmt(conf, 3)} / m ${fmt(margin, 3)}`;
}

function modelScore(alert?: AlertRow) {
  if (!alert || !Number.isFinite(Number(alert.model_score))) return '';
  const score = Number(alert.model_score).toFixed(3);
  const thr = Number.isFinite(Number(alert.model_threshold)) ? Number(alert.model_threshold).toFixed(3) : '-';
  return `${score}/${thr}`;
}

function durationText(ms: number) {
  const totalMinutes = Math.max(0, Math.ceil(ms / 60000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours <= 0) return `${minutes}m`;
  if (minutes <= 0) return `${hours}h`;
  return `${hours}h${minutes}m`;
}

function cooldownStatus(alert?: AlertRow, cooldownHours = 4, nowMs = 0) {
  const hours = Number.isFinite(cooldownHours) ? cooldownHours : 4;
  if (!alert) return `${fmt(hours, 1)}h冷却`;
  if (alert.level !== 'early_alert' && alert.level !== 'short_signal') return '-';
  if (!nowMs) return '-';
  const next = Number(alert.decision_time) + hours * 3_600_000;
  const left = next - nowMs;
  return left > 0 ? `冷却${durationText(left)}` : '可再触发';
}

function longCooldownStatus(row: LongRow, cooldownHours = 2, nowMs = 0) {
  const last = Number(row.long_last_signal_time || 0);
  if (!last) return '待首信号';
  if (!nowMs) return '-';
  const hours = Number.isFinite(cooldownHours) ? cooldownHours : 2;
  const next = last + hours * 3_600_000;
  const left = next - nowMs;
  return left > 0 ? `冷却${durationText(left)}` : '可再触发';
}

function seqText(occurrence?: number) {
  return occurrence && occurrence > 0 ? `第${occurrence}次` : '';
}

function mlInfo(evidence?: string[]) {
  const ev = evidence || [];
  const tier = ev.find((e) => e.startsWith('置信='))?.split('=')[1] || '';
  const score = ev.find((e) => e.startsWith('ML') && e.includes('分='))?.split('=')[1] || '';
  return { tier, score };
}

function signalClass(level?: string) {
  if (level === 'short_signal') return 'signal-short';
  if (level === 'early_alert') return 'signal-early';
  if (level === 'long_signal') return 'signal-long';
  if (level === 'long_timeout') return 'signal-long-timeout';
  if (level === 'fallback_alert') return 'signal-fallback';
  return 'signal-idle';
}

function sigShort(level?: string) {
  if (level === 'early_alert') return '顶';
  if (level === 'short_signal') return '空';
  if (level === 'long_signal') return '多';
  if (level === 'long_timeout') return '超';
  if (level === 'fallback_alert') return '兜';
  return '?';
}

function sigColor(level?: string) {
  if (level === 'short_signal') return '#ff4f70';
  if (level === 'early_alert') return '#ffbf4a';
  if (level === 'long_signal') return '#34d399';
  if (level === 'long_timeout') return '#94a3b8';
  if (level === 'fallback_alert') return '#d86cff';
  return '#7dd3fc';
}

export default function HunterDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string>('');
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [nowMs, setNowMs] = useState<number>(0);
  const [model, setModel] = useState<ModelMeta | null>(null);
  const [longRows, setLongRows] = useState<LongRow[]>([]);
  const [pumpHistory, setPumpHistory] = useState<PumpRow[]>([]);
  const [longHistory, setLongHistory] = useState<LongRow[]>([]);

  async function refresh() {
    try {
      const [summary, liquidity, pumps, alerts, backtests] = await Promise.all([
        api<Summary>('/api/hunter/summary'),
        api<{ rows: LiquidityRow[] }>('/api/hunter/liquidity?limit=120'),
        api<{ rows: PumpRow[] }>('/api/hunter/pumps?limit=120'),
        api<{ rows: AlertRow[] }>('/api/hunter/alerts?limit=80'),
        api<{ rows: BacktestRow[] }>('/api/hunter/backtests?limit=20'),
      ]);
      const visibleAlerts = alerts.rows.filter((alert) => alert.level !== 'long_invalid');
      setData({ summary, liquidity: liquidity.rows, pumps: pumps.rows, alerts: visibleAlerts, backtests: backtests.rows });
      const now = Date.now();
      setUpdatedAt(new Date(now));
      setNowMs(now);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    try {
      setLongRows((await api<{ rows: LongRow[] }>('/api/hunter/long?limit=60')).rows);
    } catch {
      /* 做多接口可选,忽略 */
    }
    try {
      const [pumpHistoryRes, longHistoryRes] = await Promise.all([
        api<{ rows: PumpRow[] }>('/api/hunter/pump-history?limit=300'),
        api<{ rows: LongRow[] }>('/api/hunter/long-history?limit=300'),
      ]);
      setPumpHistory(pumpHistoryRes.rows || []);
      setLongHistory(longHistoryRes.rows || []);
    } catch {
      /* 历史监管接口可选,忽略 */
    }
    try {
      setModel(await api<ModelMeta>('/api/hunter/model'));
    } catch {
      /* 模型接口可选,忽略 */
    }
  }

  useEffect(() => {
    const tick = () => { void refresh(); };
    const initial = window.setTimeout(tick, 0);
    const id = window.setInterval(tick, 10000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(id);
    };
  }, []);

  const alertsBySymbol = useMemo(() => {
    const map = new Map<string, AlertRow[]>();
    for (const alert of data?.alerts || []) {
      const arr = map.get(alert.symbol) || [];
      arr.push(alert);
      map.set(alert.symbol, arr);
    }
    for (const arr of map.values()) arr.sort((a, b) => Number(b.decision_time) - Number(a.decision_time));
    return map;
  }, [data?.alerts]);

  const monitorRows = useMemo<MonitorRow[]>(() => {
    const rows = (data?.pumps || []).map((pump) => {
      const history = alertsBySymbol.get(pump.symbol) || [];
      const latestAlert = history[0];
      return {
        ...pump,
        latestAlert,
        history,
        drawdownPct: drawdown(pump.high_price, pump.current_price),
      };
    });

    // 按最新信号触发时间倒序：最新信号永远在最上；无信号的排到最后
    rows.sort((a, b) => {
      const at = Number(a.latestAlert?.decision_time || 0);
      const bt = Number(b.latestAlert?.decision_time || 0);
      if (at !== bt) return bt - at;
      return b.drawdownPct - a.drawdownPct;
    });
    return rows;
  }, [alertsBySymbol, data?.pumps]);

  const stats = useMemo(() => {
    const summary = data?.summary || {};
    const tables = summary.tables || {};
    const activeSignals = monitorRows.filter((row) => row.latestAlert).length;
    return [
      { label: '正式监管', value: summary.active_pump_events ?? monitorRows.length, tone: 'cyan' },
      { label: '影子观察', value: summary.shadow_pump_events ?? 0, tone: 'neutral' },
      { label: '带信号', value: activeSignals, tone: 'red' },
      { label: '报警记录', value: tables.alerts ?? 0, tone: 'amber' },
      { label: 'K线库存', value: fmt(tables.candles ?? 0, 0), tone: 'green' },
      { label: '最近扫描', value: date(summary.latest_data_cutoff_time_iso || summary.latest_snapshot_time_iso), tone: 'neutral' },
      { label: '最近信号', value: date(summary.latest_alert_time_iso), tone: 'neutral' },
    ];
  }, [data?.summary, monitorRows]);
  const strategy = data?.summary?.strategy;
  const rawCooldownHours = Number(strategy?.multi_signal_cooldown_hours ?? 4);
  const cooldownHours = Number.isFinite(rawCooldownHours) ? rawCooldownHours : 4;
  const rawLongCooldownHours = Number(strategy?.long_signal_cooldown_hours ?? 2);
  const longCooldownHours = Number.isFinite(rawLongCooldownHours) ? rawLongCooldownHours : 2;

  return (
    <main className="hunter-shell">
      <header className="hunter-header">
        <div className="header-copy">
          <p className="eyebrow">BINANCE USD-M FUTURES</p>
          <h1>合约主力动向监控</h1>
          <p className="subtitle">主力异动 / 多空信号 / 4h 冷却确认 / 流动性雷达</p>
        </div>
        <div className="header-actions">
          <a className="header-link" href="#watch-history">监管记录</a>
          <button type="button" onClick={refresh} aria-label="刷新数据">
            刷新
          </button>
          <span>{updatedAt ? `更新 ${updatedAt.toLocaleTimeString()}` : '等待数据'}</span>
        </div>
      </header>

      <ModelBar model={model} nowMs={nowMs} />
      <LifecycleModelBar model={model} />
      <LifecycleStrategyBar strategy={strategy} />

      {error ? (
        <div className="error-box">
          数据源不可用：{error}。请确认 Python 服务 `python run.py web --host 127.0.0.1 --port 8787` 已启动。
        </div>
      ) : null}

      <section className="metric-grid" aria-label="运行状态">
        {stats.map((item) => (
          <div className={`metric-cell tone-${item.tone}`} key={item.label}>
            <span>{item.label}</span>
            <strong>{String(item.value)}</strong>
          </div>
        ))}
      </section>

      <section className="monitor-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">ACTIVE CONTRACTS</p>
            <h2>正在监控的合约</h2>
          </div>
          <span className="section-count">{monitorRows.length} symbols</span>
        </div>

        <div className="monitor-board">
          {monitorRows.length ? monitorRows.map((row) => (
            <MonitorContract key={row.symbol} row={row} cooldownHours={cooldownHours} nowMs={nowMs} />
          )) : <div className="empty-state">暂无入池合约</div>}
        </div>
      </section>

      <LongWatchPanel rows={longRows} cooldownHours={longCooldownHours} nowMs={nowMs} />
      <WatchHistoryPanel pumps={pumpHistory} longs={longHistory} />

      <section className="split-grid">
        <DataSection title="近期流动性 TopN">
          <table>
            <thead>
              <tr>
                <th>Rank</th><th>Symbol</th><th>15m</th><th>30m</th><th>4h</th><th>12h</th><th>1d</th><th>15m额</th><th>30m额</th><th>15m量比</th><th>30m量比</th><th>状态</th>
              </tr>
            </thead>
            <tbody>{(data?.liquidity || []).map((r) => (
              <tr key={r.symbol}>
                <td>{r.rank}</td>
                <td><b>{r.symbol}</b></td>
                <td>{pct(r.pct_15m)}</td>
                <td>{pct(r.pct_30m)}</td>
                <td>{pct(r.pct_4h)}</td>
                <td>{pct(r.pct_12h)}</td>
                <td>{pct(r.pct_1d)}</td>
                <td>{fmt(r.quote_volume_15m / 1e6)}M</td>
                <td>{fmt(r.quote_volume_30m / 1e6)}M</td>
                <td>{fmt(r.volume_ratio_15m)}x</td>
                <td>{fmt(r.volume_ratio_30m)}x</td>
                <td>{r.pump_qualified ? <span className="badge badge-active">入池</span> : '-'}</td>
              </tr>
            ))}</tbody>
          </table>
        </DataSection>

        <DataSection title="最近信号">
          <table className="compact-table">
            <thead>
              <tr>
                <th>Time</th><th>Level</th><th>Symbol</th><th>Price</th><th>Invalid</th><th>空间</th><th>量比</th><th>冷却</th>
              </tr>
            </thead>
            <tbody>{(data?.alerts || []).map((r) => (
              <tr key={r.alert_id}>
                <td>{date(r.decision_time)}</td>
                <td>
                  <span className={`badge ${signalClass(r.level)}`}>{[signalLabel(r.level), seqText(r.occurrence)].filter(Boolean).join(' ')}</span>
                  {r.category ? <span className="badge badge-cat">{r.category}</span> : null}
                  {mlInfo(r.evidence).tier === '高置信' ? <span className="badge badge-hi">高置信</span> : null}
                  {mlInfo(r.evidence).tier === '普通观察' ? <span className="badge badge-watch">普通观察</span> : null}
                  {mlInfo(r.evidence).score ? <span className="ml-score">分{mlInfo(r.evidence).score}</span> : null}
                  {r.lifecycle_mode || r.behavior_state || r.model_name ? (
                    <div className="lifecycle-inline">
                      {[r.signal_interval, lifecycleModeText(r.lifecycle_mode), behaviorText(r.behavior_state), r.model_name, modelScore(r)].filter(Boolean).join(' / ')}
                    </div>
                  ) : null}
                </td>
                <td><b>{r.symbol}</b></td>
                <td>{fmt(r.price)}</td>
                <td>{fmt(r.invalidation_price)}</td>
                <td>{fmt(r.remaining_downside_pct)}%</td>
                <td>{fmt(r.volume_ratio)}x</td>
                <td><span className="cooldown-pill">{cooldownStatus(r, cooldownHours, nowMs)}</span></td>
              </tr>
            ))}</tbody>
          </table>
        </DataSection>
      </section>

      <DataSection title="回测记录">
        <table>
          <thead>
            <tr><th>Run</th><th>Created</th><th>Days</th><th>Shorts</th><th>30m胜率</th><th>30m均值</th><th>Alerts</th></tr>
          </thead>
          <tbody>{(data?.backtests || []).map((r) => {
            const m = r.metrics || {};
            const winRate30m = Number(m.win_rate_30m ?? 0);
            const avgRet30m = Number(m.avg_ret_30m ?? 0);
            return (
              <tr key={r.run_id}>
                <td>{r.run_id}</td>
                <td>{r.created_at}</td>
                <td>{r.days}</td>
                <td>{m.short_signals ?? '-'}</td>
                <td>{fmt(winRate30m * 100)}%</td>
                <td>{fmt(avgRet30m * 100)}%</td>
                <td>{m.alerts ?? '-'}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </DataSection>
    </main>
  );
}

function dayStr(iso?: string) {
  if (!iso) return '-';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '-' : d.toISOString().slice(0, 10);
}

function LifecycleStrategyBar({ strategy }: { strategy?: StrategyMeta }) {
  const rawCooldown = Number(strategy?.multi_signal_cooldown_hours ?? 2);
  const cooldown = Number.isFinite(rawCooldown) ? rawCooldown : 2;
  return (
    <div className="strategy-bar">
      <span>策略：{strategy?.strategy_version || strategy?.mode || 'unknown'}</span>
      <span>做多：{strategy?.long_interval || '5m'} 收线</span>
      <span>顶部/做空：{strategy?.confirm_interval || '15m'} 收线</span>
      <span>同类信号冷却：{fmt(cooldown, 1)}h</span>
      <span>做多重复冷却：{fmt(strategy?.long_signal_cooldown_hours ?? 2, 1)}h</span>
      <span>PumpWatch top/short 门槛：涨过 {fmt(strategy?.lifecycle_pump_signal_min_gain_pct ?? 0, 1)}%</span>
      <span>high-pump：{strategy?.lifecycle_high_pump_enabled ? 'on' : 'off'} / {fmt(strategy?.lifecycle_high_pump_min_gain_pct ?? 40, 1)}%</span>
      <span>long 派生做空门槛：涨过 {fmt(strategy?.lifecycle_long_watch_min_gain_pct ?? 15, 1)}%</span>
      <span>回到起涨区踢出：空间 &lt; {fmt(strategy?.lifecycle_min_remaining_pct ?? 5, 1)}%</span>
      <span>{strategy?.long_enabled ? '做多监测开启' : '仅做空监测'}</span>
    </div>
  );
}

function LifecycleModelBar({ model }: { model: ModelMeta | null }) {
  const lifecycle = model?.lifecycle;
  if (!lifecycle) return null;
  return (
    <div className={`model-bar ${model?.lifecycle_ready ? '' : 'warn'}`}>
      <span className="mb-item">
        生命周期模型：{lifecycle.strategy_version || 'lifecycle_expert'} | {lifecycle.runtime?.long_interval || '5m'} 做多 / {lifecycle.runtime?.expert_interval || '15m'} 顶部做空 / 冷却 {fmt(lifecycle.runtime?.multi_signal_cooldown_hours ?? 2, 1)}h
      </span>
      <span className="mb-item">
        数据 {dayStr(lifecycle.trained_data?.data_start)} ~ {dayStr(lifecycle.trained_data?.data_end)} | symbols {lifecycle.trained_data?.symbols ?? '-'} | long q90 {fmt(lifecycle.long_score?.threshold, 3)} q95 {fmt(lifecycle.long_score?.threshold_high, 3)}
      </span>
      <span className="mb-item">
        high-pump {lifecycle.runtime?.high_pump_enabled ? 'on' : 'off'} / {fmt(lifecycle.runtime?.high_pump_min_gain_pct ?? 40, 1)}% | PumpWatch signal min {fmt(lifecycle.runtime?.pump_signal_min_gain_pct ?? 0, 1)}%
      </span>
      <span className="mb-item">专家 {Object.keys(lifecycle.models || {}).join(' / ')}</span>
    </div>
  );
}

function ModelBar({ model, nowMs }: { model: ModelMeta | null; nowMs: number }) {
  if (!model) return null;
  if (!model.ready) {
    return (
      <div className="model-bar warn">
        ⚠️ ML 模型未加载{model.error ? `(${model.error})` : ''} —— 当前无 ML 信号,需本地训练并推送模型文件
      </div>
    );
  }
  const end = model.data_end ? new Date(model.data_end) : null;
  const staleDays = end && !Number.isNaN(end.getTime()) && nowMs ? Math.floor((nowMs - end.getTime()) / 86400000) : null;
  const stale = staleDays != null && staleDays > 30;
  return (
    <div className={`model-bar ${stale ? 'warn' : ''}`}>
      <span className="mb-item">ML 模型 · 数据 {dayStr(model.data_start)} ~ {dayStr(model.data_end)} · 训练于 {dayStr(model.trained_at)}</span>
      <span className="mb-item">下跌启动 AUC {model.dump?.val_auc ?? '-'} · 见顶 AUC {model.top?.val_auc ?? '-'} · 做多 AUC {model.long?.val_auc ?? '-'}</span>
      {stale ? <span className="mb-stale">⚠️ 模型数据已过期 {staleDays} 天,建议本地重训后 push 更新</span> : null}
    </div>
  );
}

function LongWatchPanel({ rows, cooldownHours, nowMs }: { rows: LongRow[]; cooldownHours: number; nowMs: number }) {
  return (
    <section className="monitor-section">
      <div className="section-heading">
        <div>
          <p className="eyebrow">LONG WATCH</p>
          <h2>做多监管池</h2>
        </div>
        <span className="section-count">{rows.length} symbols</span>
      </div>
      {rows.length ? (
        <table className="long-table">
          <thead>
            <tr><th>Symbol</th><th>入场</th><th>现价</th><th>距入场</th><th>最高</th><th>做多信号</th><th>冷却</th><th></th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const chg = r.entry_price > 0 ? (r.current_price / r.entry_price - 1) * 100 : 0;
              return (
                <tr key={r.symbol}>
                  <td><b>{r.symbol}</b></td>
                  <td>{fmt(r.entry_price, 6)}</td>
                  <td>{fmt(r.current_price, 6)}</td>
                  <td style={{ color: chg >= 0 ? '#34d399' : '#ff4f70' }}>{chg >= 0 ? '+' : ''}{fmt(chg)}%</td>
                  <td>{fmt(r.high_price, 6)}</td>
                  <td>{r.long_signal_seq > 0 ? `已发${r.long_signal_seq}次` : '待触发'}</td>
                  <td><span className="cooldown-pill">{longCooldownStatus(r, cooldownHours, nowMs)}</span></td>
                  <td><a href={`https://www.binance.com/zh-CN/futures/${r.symbol}`} target="_blank" rel="noreferrer">合约↗</a></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : <div className="empty-state">暂无做多监管币</div>}
    </section>
  );
}

function WatchHistoryPanel({ pumps, longs }: { pumps: PumpRow[]; longs: LongRow[] }) {
  const closedPumps = pumps.filter((row) => row.status !== 'active').length;
  const closedLongs = longs.filter((row) => row.status !== 'active').length;
  return (
    <section className="monitor-section history-section" id="watch-history">
      <div className="section-heading">
        <div>
          <p className="eyebrow">WATCH HISTORY</p>
          <h2>监管记录</h2>
        </div>
        <span className="section-count">
          Pump {pumps.length} / Long {longs.length} / 已踢出 {closedPumps + closedLongs}
        </span>
      </div>
      <div className="history-grid">
        <div className="history-table">
          <div className="section-heading small"><h2>PumpWatch 历史</h2></div>
          <div className="table-wrap">
            <table className="compact-table">
              <thead>
                <tr><th>Symbol</th><th>状态</th><th>来源</th><th>入池</th><th>最近</th><th>最大涨幅</th><th>现价</th><th>阶段</th><th>记录</th></tr>
              </thead>
              <tbody>{pumps.map((row) => (
                <tr key={row.event_id || `${row.symbol}-${row.first_seen}`}>
                  <td><b>{row.symbol}</b></td>
                  <td><span className={`badge ${row.status === 'active' ? 'badge-active' : 'badge-closed'}`}>{row.status || '-'}</span></td>
                  <td>{row.trigger_window}</td>
                  <td>{date(row.first_seen)}</td>
                  <td>{date(row.last_seen)}</td>
                  <td>{pct(row.max_gain_pct)}</td>
                  <td>{fmt(row.current_price, 6)}</td>
                  <td>{lifecycleModeText(row.lifecycle_mode)} / {behaviorText(row.behavior_state)}</td>
                  <td className="history-note">{(row.evidence || []).slice(-2).join(' / ') || '-'}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
        <div className="history-table">
          <div className="section-heading small"><h2>LongWatch 历史</h2></div>
          <div className="table-wrap">
            <table className="compact-table">
              <thead>
                <tr><th>Symbol</th><th>状态</th><th>入池</th><th>最近</th><th>入场</th><th>现价</th><th>最高</th><th>信号</th><th>原因</th></tr>
              </thead>
              <tbody>{longs.map((row) => {
                const chg = row.entry_price > 0 ? (row.current_price / row.entry_price - 1) * 100 : 0;
                return (
                  <tr key={row.event_id || `${row.symbol}-${row.first_seen}`}>
                    <td><b>{row.symbol}</b></td>
                    <td><span className={`badge ${row.status === 'active' ? 'badge-active' : 'badge-closed'}`}>{row.status || '-'}</span></td>
                    <td>{date(row.first_seen)}</td>
                    <td>{date(row.last_seen)}</td>
                    <td>{fmt(row.entry_price, 6)}</td>
                    <td>{fmt(row.current_price, 6)} <span className={chg >= 0 ? 'trend-up' : 'trend-down'}>{chg >= 0 ? '+' : ''}{fmt(chg)}%</span></td>
                    <td>{fmt(row.high_price, 6)}</td>
                    <td>{row.long_signal_seq || 0} / {date(row.long_last_signal_time)}</td>
                    <td className="history-note">{row.exit_reason || (row.evidence || []).slice(-1)[0] || '-'}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  );
}

function MonitorContract({ row, cooldownHours, nowMs }: { row: MonitorRow; cooldownHours: number; nowMs: number }) {
  const alert = row.latestAlert;
  const ml = mlInfo(alert?.evidence);
  const repeated = Number(alert?.occurrence || 0) > 1;
  const [open, setOpen] = useState(false);
  const [candles, setCandles] = useState<Candle[] | null>(null);
  const [loading, setLoading] = useState(false);
  const tradeUrl = `https://www.binance.com/zh-CN/futures/${row.symbol}`;
  const routeText = routeCompact(row);
  const routeDetail = routeSummary(row);

  async function toggleChart() {
    const next = !open;
    setOpen(next);
    if (next && candles === null) {
      setLoading(true);
      try {
        const q = new URLSearchParams({ symbol: row.symbol, interval: '15m', limit: '160' });
        if (row.first_seen) q.set('start_time', String(row.first_seen));
        const res = await api<{ rows: Candle[] }>(`/api/hunter/candles?${q.toString()}`);
        setCandles(res.rows || []);
      } catch {
        setCandles([]);
      } finally {
        setLoading(false);
      }
    }
  }

  return (
    <article className={`monitor-row ${alert ? 'has-signal' : ''} ${repeated ? 'repeat-signal' : ''}`}>
      <div className="contract-main">
        <div className="symbol-line">
          <strong>{row.symbol}</strong>
          <span>{row.trigger_window}</span>
          <span className="lifecycle-chip">{lifecycleModeText(row.lifecycle_mode)} / {behaviorText(row.behavior_state)}</span>
          {row.long_derived_watch ? <span className="stage-chip">Long派生</span> : null}
          {row.is_formal_watch === false ? <span className="stage-chip stage-shadow">影子观察</span> : null}
          {routeText ? <span className="route-chip" title={routeDetail}>{routeText}</span> : null}
          <span className="contract-actions">
            <button type="button" className="link-btn" onClick={toggleChart}>{open ? '收起K线' : '15m K线'}</button>
            <a className="trade-link" href={tradeUrl} target="_blank" rel="noreferrer">交易 ↗</a>
            {alert?.model_name ? <em className="cat-tag">{alert.signal_interval || '15m'} / {alert.model_name} / {modelScore(alert)}</em> : null}
          </span>
        </div>
        <div className="contract-stats">
          <Metric label="最大涨幅" value={pctText(row.max_gain_pct)} tone="up" />
          <Metric label="高点回撤" value={pctText(row.drawdownPct)} tone={row.drawdownPct >= 12 ? 'down' : 'warm'} />
          <Metric label="High" value={fmt(row.high_price, 6)} />
          <Metric label="Current" value={fmt(row.current_price, 6)} />
        </div>
      </div>

      <div className={`inline-signal ${signalClass(alert?.level)}`}>
        <div className="signal-topline">
          <span>
            {[signalLabel(alert?.level), seqText(alert?.occurrence)].filter(Boolean).join(' ')}
            {alert?.category ? <em className="cat-tag">{alert.category}</em> : null}
            {ml.tier === '高置信' ? <em className="cat-tag hi">高置信</em> : null}
            {ml.tier === '普通观察' ? <em className="cat-tag watch">普通观察</em> : null}
            {ml.score ? <em className="cat-tag">分{ml.score}</em> : null}
          </span>
          <strong>{cooldownStatus(alert, cooldownHours, nowMs)}</strong>
        </div>
        <div className={`signal-time-big ${alert ? '' : 'muted'}`}>{alert ? date(alert.decision_time) : '待触发'}</div>
        {alert ? (
          <>
            <div className="signal-price">
              <b>{fmt(alert.price, 6)}</b>
              <span>空间 {fmt(alert.remaining_downside_pct)}% · 量比 {fmt(alert.volume_ratio)}x</span>
            </div>
            <div className="signal-rule">
              <span>{fmt(cooldownHours, 1)}h 冷却</span>
              <span>{repeated ? '重复确认' : '首次信号'}</span>
            </div>
            {row.history.length > 1 ? (
              <div className="signal-history">
                {row.history.map((h) => (
                  <span className="hist-chip" key={h.alert_id} style={{ color: sigColor(h.level) }}>
                    {sigShort(h.level)}{h.occurrence || ''} · {timeOnly(h.decision_time)} · {fmt(h.price, 4)}
                  </span>
                ))}
              </div>
            ) : (
              <p>{(alert.evidence || []).slice(0, 2).join(' / ')}</p>
            )}
          </>
        ) : (
          <div className="signal-price">
            <span>失效 {date(row.expires_at)}</span>
          </div>
        )}
      </div>

      {open ? (
        <div className="kline-panel">
          {loading ? (
            <div className="kline-empty">加载中…</div>
          ) : candles && candles.length ? (
            <MiniKline candles={candles} anchor={row.anchor_price} high={row.high_price} signals={row.history} />
          ) : (
            <div className="kline-empty">暂无 15m K 线(该合约刚入池或尚未落库)</div>
          )}
        </div>
      ) : null}
    </article>
  );
}

function MiniKline({ candles, anchor, high, signals }: { candles: Candle[]; anchor?: number; high?: number; signals?: AlertRow[] }) {
  const W = Math.max(candles.length * 7, 160);
  const H = 150;
  const pad = 8;
  const refs = [anchor, high].filter((v): v is number => typeof v === 'number' && v > 0);
  const lo = Math.min(...candles.map((c) => c.low), ...refs);
  const hi = Math.max(...candles.map((c) => c.high), ...refs);
  const span = hi - lo || 1;
  const y = (p: number) => pad + (1 - (p - lo) / span) * (H - 2 * pad);
  const cw = (W - 2 * pad) / candles.length;
  const xAt = (i: number) => pad + i * cw + cw / 2;
  return (
    <div className="kline-wrap">
      <svg className="kline-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {typeof anchor === 'number' && anchor > 0 ? <line className="kline-ref anchor" x1={0} x2={W} y1={y(anchor)} y2={y(anchor)} /> : null}
        {typeof high === 'number' && high > 0 ? <line className="kline-ref high" x1={0} x2={W} y1={y(high)} y2={y(high)} /> : null}
        {candles.map((c, i) => {
          const x = xAt(i);
          const up = c.close >= c.open;
          const top = y(Math.max(c.open, c.close));
          const bot = y(Math.min(c.open, c.close));
          const bw = Math.max(cw * 0.62, 1.2);
          return (
            <g key={c.open_time} className={up ? 'k-up' : 'k-down'}>
              <line x1={x} x2={x} y1={y(c.high)} y2={y(c.low)} />
              <rect x={x - bw / 2} width={bw} y={top} height={Math.max(bot - top, 1)} />
            </g>
          );
        })}
        {(signals || []).map((s) => {
          const dt = Number(s.decision_time);
          const idx = candles.findIndex((c) => c.open_time <= dt && dt < c.open_time + 900000);
          if (idx < 0) return null;
          const x = xAt(idx);
          const color = sigColor(s.level);
          return (
            <g key={s.alert_id}>
              <line x1={x} x2={x} y1={pad} y2={H - pad} stroke={color} strokeOpacity={0.5} strokeWidth={1} />
              <rect x={x - 3} y={2} width={6} height={6} fill={color} />
            </g>
          );
        })}
      </svg>
      <div className="kline-legend">
        <span>起涨 {date(candles[0]?.open_time)}</span>
        <span>{candles.length}根15m · 竖标=信号(红空/黄顶/紫兜) · 虚线灰=锚点 黄=高点</span>
      </div>
    </div>
  );
}

function Metric({ label, value, tone = 'neutral' }: { label: string; value: string; tone?: 'up' | 'down' | 'warm' | 'neutral' }) {
  return (
    <span className={`mini-metric mini-${tone}`}>
      <em>{label}</em>
      <b>{value}</b>
    </span>
  );
}

function DataSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="data-section">
      <div className="section-heading small">
        <h2>{title}</h2>
      </div>
      <div className="table-wrap">{children}</div>
    </section>
  );
}
