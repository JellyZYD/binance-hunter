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

function pct(value: unknown) {
  const n = Number(value);
  if (!Number.isFinite(n)) return <span>-</span>;
  return <span className={n >= 0 ? 'trend-up' : 'trend-down'}>{n.toFixed(2)}%</span>;
}

function date(value?: number | string | null) {
  if (!value) return '-';
  const d = typeof value === 'number' ? new Date(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString();
}

export default function HunterDashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string>('');
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  async function refresh() {
    try {
      const [summary, liquidity, pumps, alerts, backtests] = await Promise.all([
        api<Summary>('/api/hunter/summary'),
        api<{ rows: LiquidityRow[] }>('/api/hunter/liquidity?limit=80'),
        api<{ rows: PumpRow[] }>('/api/hunter/pumps?limit=80'),
        api<{ rows: AlertRow[] }>('/api/hunter/alerts?limit=50'),
        api<{ rows: BacktestRow[] }>('/api/hunter/backtests?limit=20'),
      ]);
      setData({ summary, liquidity: liquidity.rows, pumps: pumps.rows, alerts: alerts.rows, backtests: backtests.rows });
      setUpdatedAt(new Date());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 10000);
    return () => window.clearInterval(id);
  }, []);

  const cards = useMemo(() => {
    const summary = data?.summary || {};
    const tables = summary.tables || {};
    return [
      ['活跃妖币', summary.active_pump_events ?? 0],
      ['报警数', tables.alerts ?? 0],
      ['流动性快照', tables.liquidity_snapshots ?? 0],
      ['K线数', tables.candles ?? 0],
      ['最近扫描', summary.latest_snapshot_time_iso || '-'],
      ['最近报警', summary.latest_alert_time_iso || '-'],
    ];
  }, [data]);

  return (
    <main className="hunter-shell">
      <header className="hunter-header">
        <div>
          <p className="eyebrow">Binance USD-M Futures</p>
          <h1>妖币冲高回落做空猎手</h1>
          <p className="subtitle">REST 扫描近期流动性 TopN，WebSocket 只用已收线 K 线盯盘，回测与实盘同一套事件引擎。</p>
        </div>
        <div className="header-actions">
          <button type="button" onClick={refresh}>刷新</button>
          <span>{updatedAt ? `更新 ${updatedAt.toLocaleTimeString()}` : '等待数据'}</span>
        </div>
      </header>

      {error ? <div className="error-box">数据源不可用：{error}。请确认 Python 服务 `python run.py web --host 127.0.0.1 --port 8787` 已启动。</div> : null}

      <section className="metric-grid">
        {cards.map(([label, value]) => (
          <div className="metric-card" key={label}>
            <span>{label}</span>
            <strong>{String(value)}</strong>
          </div>
        ))}
      </section>

      <DataSection title="近期流动性 TopN">
        <table>
          <thead><tr><th>Rank</th><th>Symbol</th><th>15m涨幅</th><th>30m涨幅</th><th>4h涨幅</th><th>12h涨幅</th><th>1d涨幅</th><th>15m额</th><th>30m额</th><th>15m量比</th><th>30m量比</th><th>状态</th></tr></thead>
          <tbody>{(data?.liquidity || []).map((r) => (
            <tr key={r.symbol}><td>{r.rank}</td><td><b>{r.symbol}</b></td><td>{pct(r.pct_15m)}</td><td>{pct(r.pct_30m)}</td><td>{pct(r.pct_4h)}</td><td>{pct(r.pct_12h)}</td><td>{pct(r.pct_1d)}</td><td>{fmt(r.quote_volume_15m / 1e6)}M</td><td>{fmt(r.quote_volume_30m / 1e6)}M</td><td>{fmt(r.volume_ratio_15m)}x</td><td>{fmt(r.volume_ratio_30m)}x</td><td>{r.pump_qualified ? <span className="badge">入池</span> : '-'}</td></tr>
          ))}</tbody>
        </table>
      </DataSection>

      <DataSection title="活跃妖币池">
        <table>
          <thead><tr><th>Symbol</th><th>触发周期</th><th>最大涨幅</th><th>High</th><th>Current</th><th>过期时间</th><th>证据</th></tr></thead>
          <tbody>{(data?.pumps || []).map((r) => (
            <tr key={r.symbol}><td><b>{r.symbol}</b></td><td>{r.trigger_window}</td><td>{pct(r.max_gain_pct)}</td><td>{fmt(r.high_price)}</td><td>{fmt(r.current_price)}</td><td>{date(r.expires_at)}</td><td>{(r.evidence || []).slice(0, 3).join('; ')}</td></tr>
          ))}</tbody>
        </table>
      </DataSection>

      <DataSection title="最近报警">
        <table>
          <thead><tr><th>Time</th><th>Level</th><th>Symbol</th><th>Price</th><th>Invalid</th><th>剩余空间</th><th>量比</th><th>证据</th></tr></thead>
          <tbody>{(data?.alerts || []).map((r) => (
            <tr key={r.alert_id}><td>{date(r.decision_time)}</td><td><span className="badge">{r.level}</span></td><td><b>{r.symbol}</b></td><td>{fmt(r.price)}</td><td>{fmt(r.invalidation_price)}</td><td>{fmt(r.remaining_downside_pct)}%</td><td>{fmt(r.volume_ratio)}x</td><td>{(r.evidence || []).slice(0, 3).join('; ')}</td></tr>
          ))}</tbody>
        </table>
      </DataSection>

      <DataSection title="回测记录">
        <table>
          <thead><tr><th>Run</th><th>Created</th><th>Days</th><th>Shorts</th><th>30m胜率</th><th>30m均值</th><th>Alerts</th></tr></thead>
          <tbody>{(data?.backtests || []).map((r) => {
            const m = r.metrics || {};
            const winRate30m = Number(m.win_rate_30m ?? 0);
            const avgRet30m = Number(m.avg_ret_30m ?? 0);
            return <tr key={r.run_id}><td>{r.run_id}</td><td>{r.created_at}</td><td>{r.days}</td><td>{m.short_signals ?? '-'}</td><td>{fmt(winRate30m * 100)}%</td><td>{fmt(avgRet30m * 100)}%</td><td>{m.alerts ?? '-'}</td></tr>;
          })}</tbody>
        </table>
      </DataSection>
    </main>
  );
}

function DataSection({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="data-section"><h2>{title}</h2><div className="table-wrap">{children}</div></section>;
}
