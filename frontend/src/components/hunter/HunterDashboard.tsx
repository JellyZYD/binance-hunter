'use client';

import { useEffect, useMemo, useState } from 'react';

type Summary = {
  tables?: Record<string, number>;
  active_pump_events?: number;
  latest_snapshot_time_iso?: string | null;
  latest_data_cutoff_time_iso?: string | null;
  latest_alert_time_iso?: string | null;
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
  symbol: string;
  trigger_window: string;
  high_price: number;
  current_price: number;
  max_gain_pct: number;
  expires_at: number;
  evidence?: string[];
  first_seen?: number;
  anchor_price?: number;
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
};

type MonitorRow = PumpRow & {
  latestAlert?: AlertRow;
  history: AlertRow[];
  drawdownPct: number;
  alertAgeMs?: number;
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
  if (level === 'fallback_alert') return '回落兜底';
  return level || '等待信号';
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
  if (level === 'fallback_alert') return 'signal-fallback';
  return 'signal-idle';
}

function sigShort(level?: string) {
  if (level === 'early_alert') return '顶';
  if (level === 'short_signal') return '空';
  if (level === 'fallback_alert') return '兜';
  return '?';
}

function sigColor(level?: string) {
  if (level === 'short_signal') return '#ff4f70';
  if (level === 'early_alert') return '#ffbf4a';
  if (level === 'fallback_alert') return '#d86cff';
  return '#7dd3fc';
}

export default function HunterDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string>('');
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [model, setModel] = useState<ModelMeta | null>(null);

  async function refresh() {
    try {
      const [summary, liquidity, pumps, alerts, backtests] = await Promise.all([
        api<Summary>('/api/hunter/summary'),
        api<{ rows: LiquidityRow[] }>('/api/hunter/liquidity?limit=120'),
        api<{ rows: PumpRow[] }>('/api/hunter/pumps?limit=120'),
        api<{ rows: AlertRow[] }>('/api/hunter/alerts?limit=80'),
        api<{ rows: BacktestRow[] }>('/api/hunter/backtests?limit=20'),
      ]);
      setData({ summary, liquidity: liquidity.rows, pumps: pumps.rows, alerts: alerts.rows, backtests: backtests.rows });
      setUpdatedAt(new Date());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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
        alertAgeMs: latestAlert ? Date.now() - Number(latestAlert.decision_time) : undefined,
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
      { label: '监控合约', value: summary.active_pump_events ?? monitorRows.length, tone: 'cyan' },
      { label: '带信号', value: activeSignals, tone: 'red' },
      { label: '报警记录', value: tables.alerts ?? 0, tone: 'amber' },
      { label: 'K线库存', value: fmt(tables.candles ?? 0, 0), tone: 'green' },
      { label: '最近扫描', value: date(summary.latest_data_cutoff_time_iso || summary.latest_snapshot_time_iso), tone: 'neutral' },
      { label: '最近信号', value: date(summary.latest_alert_time_iso), tone: 'neutral' },
    ];
  }, [data?.summary, monitorRows]);

  return (
    <main className="hunter-shell">
      <header className="hunter-header">
        <div className="header-copy">
          <p className="eyebrow">BINANCE USD-M FUTURES</p>
          <h1>妖币急跌做空监控台</h1>
          <p className="subtitle"> active watchlist / signal board / liquidity radar </p>
        </div>
        <div className="header-actions">
          <button type="button" onClick={refresh} aria-label="刷新数据">
            刷新
          </button>
          <span>{updatedAt ? `更新 ${updatedAt.toLocaleTimeString()}` : '等待数据'}</span>
        </div>
      </header>

      <ModelBar model={model} />

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
            <MonitorContract key={row.symbol} row={row} />
          )) : <div className="empty-state">暂无入池合约</div>}
        </div>
      </section>

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
                <th>Time</th><th>Level</th><th>Symbol</th><th>Price</th><th>Invalid</th><th>空间</th><th>量比</th>
              </tr>
            </thead>
            <tbody>{(data?.alerts || []).map((r) => (
              <tr key={r.alert_id}>
                <td>{date(r.decision_time)}</td>
                <td>
                  <span className={`badge ${signalClass(r.level)}`}>{[signalLabel(r.level), seqText(r.occurrence)].filter(Boolean).join(' ')}</span>
                  {r.category ? <span className="badge badge-cat">{r.category}</span> : null}
                  {mlInfo(r.evidence).tier === '高置信' ? <span className="badge badge-hi">高置信</span> : null}
                  {mlInfo(r.evidence).score ? <span className="ml-score">分{mlInfo(r.evidence).score}</span> : null}
                </td>
                <td><b>{r.symbol}</b></td>
                <td>{fmt(r.price)}</td>
                <td>{fmt(r.invalidation_price)}</td>
                <td>{fmt(r.remaining_downside_pct)}%</td>
                <td>{fmt(r.volume_ratio)}x</td>
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

function ModelBar({ model }: { model: ModelMeta | null }) {
  if (!model) return null;
  if (!model.ready) {
    return (
      <div className="model-bar warn">
        ⚠️ ML 模型未加载{model.error ? `(${model.error})` : ''} —— 当前无 ML 信号,需本地训练并推送模型文件
      </div>
    );
  }
  const end = model.data_end ? new Date(model.data_end) : null;
  const staleDays = end && !Number.isNaN(end.getTime()) ? Math.floor((Date.now() - end.getTime()) / 86400000) : null;
  const stale = staleDays != null && staleDays > 30;
  return (
    <div className={`model-bar ${stale ? 'warn' : ''}`}>
      <span className="mb-item">ML 模型 · 数据 {dayStr(model.data_start)} ~ {dayStr(model.data_end)} · 训练于 {dayStr(model.trained_at)}</span>
      <span className="mb-item">下跌启动 AUC {model.dump?.val_auc ?? '-'} · 见顶 AUC {model.top?.val_auc ?? '-'}</span>
      {stale ? <span className="mb-stale">⚠️ 模型数据已过期 {staleDays} 天,建议本地重训后 push 更新</span> : null}
    </div>
  );
}

function MonitorContract({ row }: { row: MonitorRow }) {
  const alert = row.latestAlert;
  const ml = mlInfo(alert?.evidence);
  const [open, setOpen] = useState(false);
  const [candles, setCandles] = useState<Candle[] | null>(null);
  const [loading, setLoading] = useState(false);
  const tradeUrl = `https://www.binance.com/zh-CN/futures/${row.symbol}`;

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
    <article className={`monitor-row ${alert ? 'has-signal' : ''}`}>
      <div className="contract-main">
        <div className="symbol-line">
          <strong>{row.symbol}</strong>
          <span>{row.trigger_window}</span>
          <span className="contract-actions">
            <button type="button" className="link-btn" onClick={toggleChart}>{open ? '收起K线' : '15m K线'}</button>
            <a className="trade-link" href={tradeUrl} target="_blank" rel="noreferrer">交易 ↗</a>
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
            {ml.score ? <em className="cat-tag">分{ml.score}</em> : null}
          </span>
        </div>
        <div className={`signal-time-big ${alert ? '' : 'muted'}`}>{alert ? date(alert.decision_time) : '待触发'}</div>
        {alert ? (
          <>
            <div className="signal-price">
              <b>{fmt(alert.price, 6)}</b>
              <span>空间 {fmt(alert.remaining_downside_pct)}% · 量比 {fmt(alert.volume_ratio)}x</span>
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
