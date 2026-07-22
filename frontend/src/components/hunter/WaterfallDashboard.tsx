'use client';

import type { ReactNode } from 'react';
import { useEffect, useMemo, useState } from 'react';

type SystemStat = {
  ts: number;
  cpu: { percent: number; cores: number; load1: number; load5: number; load15: number };
  memory: { total_mb: number; used_mb: number; percent: number; swap_used_mb: number };
  disk: { total_gb: number; used_gb: number; free_gb: number; percent: number };
  network: { rx_kbps: number; tx_kbps: number; rx_total_gb: number; tx_total_gb: number };
  binance: { connected: boolean; last_candle_age_sec: number | null; candles_1m: number; candle_span_days?: number };
  data: { db_mb: number; micro: { mb: number; files: number; span_hours: number } };
  monitor?: { rss_mb?: number; events?: number; open_positions?: number; universe?: number; watch_symbols?: number; age_sec?: number; alive?: boolean } | null;
};

type StrategyAccount = {
  account_id: string;
  strategy: string;
  label?: string;
  strategy_label: string;
  sizing_mode?: string;
  base_margin_fraction?: number;
  leverage?: number;
  current_drawdown_pct?: number;
  max_drawdown_pct?: number;
  open_positions: number;
  closed_positions: number;
  signals: number;
  win_rate: number;
  avg_pnl_pct: number;
  paper_initial_balance_usdt?: number;
  paper_realized_pnl_usdt?: number;
  paper_unrealized_pnl_usdt?: number;
  paper_equity_usdt?: number;
  paper_used_margin_usdt?: number;
};

type WaterfallSummary = {
  watch: number;
  open_positions: number;
  closed_positions: number;
  signals: number;
  paper_pnl_usdt: number;
  paper_realized_pnl_usdt?: number;
  paper_unrealized_pnl_usdt?: number;
  paper_initial_balance_usdt?: number;
  paper_equity_usdt?: number;
  paper_used_margin_usdt?: number;
  paper_free_balance_usdt?: number;
  avg_pnl_pct: number;
  win_rate: number;
  active_strategy?: string;
  accounts?: StrategyAccount[];
  config?: Record<string, unknown>;
};

type WatchRow = {
  symbol: string;
  strategy: string;
  status: string;
  family: string;
  last_time: number;
  last_price: number;
  ret_30m: number;
  ret_2h: number;
  ret_4h: number;
  ret_24h: number;
  runup_24h: number;
  dd_from_24h_high: number;
  qv30: number;
  volr20: number;
  volr5_20: number;
  tsell: number;
  updated_time: number;
};

type PositionRow = {
  position_id: string;
  symbol: string;
  strategy: string;
  family: string;
  rule: string;
  exit_profile: string;
  status: string;
  side: string;
  entry_time: number;
  entry_price: number;
  notional_usdt: number;
  margin_usdt: number;
  leverage: number;
  capital_fraction: number;
  stop_price: number;
  best_price: number;
  worst_price: number;
  trail_price: number;
  mark_price?: number;
  unrealized_pnl_pct?: number;
  unrealized_pnl_usdt?: number;
  margin_roi_pct?: number;
  exit_time?: number | null;
  exit_price: number;
  pnl_pct: number;
  pnl_usdt: number;
  exit_reason: string;
  updated_time: number;
};

type SignalRow = {
  signal_id: string;
  position_id: string;
  symbol: string;
  strategy: string;
  action: string;
  family: string;
  rule: string;
  decision_time: number;
  price: number;
  stop_price: number;
  pnl_pct: number;
  confidence: number;
  tier?: string;
  notional_usdt?: number;
  margin_usdt?: number;
  leverage?: number;
  account_equity_usdt?: number;
};

type ReplayResult = {
  result_type?: string;
  mode?: string;
  variant?: string;
  families?: string[];
  symbols?: number;
  start?: string;
  end?: string;
  signals?: number;
  trades?: number;
  closed_trades?: number;
  trades_per_day?: number;
  win_rate?: number;
  avg_pnl_pct?: number;
  median_pnl_pct?: number;
  median_symbol_pnl_pct?: number;
  profit_factor?: number | null;
  avg_mae_pct?: number;
  avg_mfe_pct?: number;
  big_3pct_rate?: number;
  big_5pct_rate?: number;
  big_3pct?: number;
  big_5pct?: number;
  updated_time?: number;
};

type LiveSummary = {
  available: boolean;
  mode: string;
  enabled: boolean;
  real_order_enabled: boolean;
  leverage: number;
  max_open_positions: number;
  max_notional_usdt: number;
  risk_per_trade: number;
  sizing_mode: string;
  base_margin_fraction: number;
  execution_policy: string;
  safe_halt_reason?: string;
  sizing?: {
    start_time: number;
    initial_equity: string;
    current_equity: string;
    peak_equity: string;
    drawdown_pct: string;
    factor: string;
  };
  service?: {
    heartbeat_time: number;
    status: string;
    pid: number;
    processed_events: number;
  };
  account?: {
    snapshot_time: number;
    wallet_balance: string;
    available_balance: string;
    margin_balance: string;
    unrealized_pnl: string;
  } | null;
  positions: Array<{
    position_id: string;
    symbol: string;
    status: string;
    quantity: string;
    entry_price: string;
    structure_stop_price: string;
    trail_price: string;
    liquidation_price: string;
    protected: number;
    updated_time: number;
  }>;
  orders: Array<{
    client_order_id: string;
    symbol: string;
    side: string;
    order_type: string;
    execution_policy: string;
    state: string;
    filled_quantity: string;
    average_price: string;
    slippage_bps: string;
    arrival_slippage_bps: string;
    signal_to_submit_ms?: number | null;
    submit_to_ack_ms?: number | null;
    submit_to_first_fill_ms?: number | null;
    signal_to_fill_ms?: number | null;
    signal_to_final_fill_ms?: number | null;
    updated_time: number;
  }>;
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

function pct(value: unknown, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  return `${(n * 100).toFixed(digits)}%`;
}

function usdt(value: unknown, digits = 2) {
  return `${fmt(value, digits)} U`;
}

function date(value?: number | string | null) {
  if (!value) return '-';
  const d = typeof value === 'number' ? new Date(value) : new Date(value);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString();
}

function actionText(action: string) {
  if (action === 'open_short') return '开空';
  if (action === 'take_profit') return '止盈';
  if (action === 'stop_loss') return '止损';
  if (action === 'timeout_exit') return '超时离场';
  return action;
}

function familyText(family: string) {
  if (family === 'post_pump') return '暴涨后瀑布';
  if (family === 'downtrend_continuation') return '下跌续瀑布';
  if (family === 'momentum_dump') return '动能转瀑布';
  if (family === 'range_breakdown') return '横盘破位';
  if (family === 'other') return '其他可吃瀑布';
  return family || '-';
}

function actionClass(action: string) {
  if (action === 'open_short') return 'wf-open';
  if (action === 'take_profit') return 'wf-profit';
  if (action === 'stop_loss') return 'wf-stop';
  return 'wf-neutral';
}

function replayTrades(row: ReplayResult) {
  return row.trades ?? row.closed_trades ?? 0;
}

function replayMedian(row: ReplayResult) {
  return row.median_pnl_pct ?? row.median_symbol_pnl_pct ?? 0;
}

function replayBig3(row: ReplayResult) {
  return row.big_3pct_rate ?? row.big_3pct ?? 0;
}

function replayBig5(row: ReplayResult) {
  return row.big_5pct_rate ?? row.big_5pct ?? 0;
}

function replayModeText(row: ReplayResult) {
  if (row.result_type === 'mode_compare' && row.mode === 'kline') return '1m 收线';
  if (row.result_type === 'mode_compare' && row.mode === 'agg') return 'agg 快触发';
  if (row.mode === 'agg_direct') return 'agg 直接回放';
  return row.mode || '-';
}

function strategyTag(strategy?: string) {
  if (!strategy) return '';
  return strategy === 'claude_board_wf_1m' ? 'Claude·冠军标签' : strategy;
}

function sizingText(account: StrategyAccount) {
  const base = `${fmt(Number(account.base_margin_fraction || 0) * 100, 1)}% / ${fmt(account.leverage || 1, 1)}x`;
  return account.sizing_mode === 'realized_drawdown_ladder' ? `${base} · 回撤缩仓梯` : `${base} · 固定仓位`;
}

function tierText(tier?: string) {
  if (tier === 'bookdepth_strong') return 'BookDepth增强';
  if (tier === 'strong') return '强信号';
  if (tier === 'normal') return '普通';
  return tier || '普通';
}

export default function WaterfallDashboard() {
  const [summary, setSummary] = useState<WaterfallSummary | null>(null);
  const [watch, setWatch] = useState<WatchRow[]>([]);
  const [openPositions, setOpenPositions] = useState<PositionRow[]>([]);
  const [closedPositions, setClosedPositions] = useState<PositionRow[]>([]);
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const [replays, setReplays] = useState<ReplayResult[]>([]);
  const [system, setSystem] = useState<SystemStat | null>(null);
  const [live, setLive] = useState<LiveSummary | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [error, setError] = useState('');

  async function refresh() {
    try {
      const [summaryRes, watchRes, openRes, closedRes, signalRes, replayRes, systemRes, liveRes] = await Promise.all([
        api<WaterfallSummary>('/api/hunter/waterfall/summary'),
        api<{ rows: WatchRow[] }>('/api/hunter/waterfall/watch?limit=450'),
        api<{ rows: PositionRow[] }>('/api/hunter/waterfall/positions?status=open&limit=100'),
        api<{ rows: PositionRow[] }>('/api/hunter/waterfall/positions?status=closed&limit=160'),
        api<{ rows: SignalRow[] }>('/api/hunter/waterfall/signals?limit=180'),
        api<{ rows: ReplayResult[] }>('/api/hunter/waterfall/replay-results?limit=12'),
        api<SystemStat>('/api/hunter/system').catch(() => null),
        api<LiveSummary>('/api/hunter/live/summary?limit=30').catch(() => null),
      ]);
      setSummary(summaryRes);
      setWatch(watchRes.rows || []);
      setOpenPositions(openRes.rows || []);
      setClosedPositions(closedRes.rows || []);
      setSignals(signalRes.rows || []);
      setReplays(replayRes.rows || []);
      setSystem(systemRes);
      setLive(liveRes);
      setUpdatedAt(new Date());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    const first = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 8000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, []);

  const newestSignals = useMemo(() => signals.slice(0, 10), [signals]);
  const topWatch = useMemo(() => {
    return [...watch]
      .sort((a, b) => {
        if (a.status !== b.status) return a.status === 'in_position' ? -1 : 1;
        return Number(b.updated_time) - Number(a.updated_time);
      })
      .slice(0, 120);
  }, [watch]);

  const cfg = summary?.config || {};
  const equity = summary?.paper_equity_usdt ?? summary?.paper_initial_balance_usdt ?? 0;
  const realized = summary?.paper_realized_pnl_usdt ?? summary?.paper_pnl_usdt ?? 0;
  const unrealized = summary?.paper_unrealized_pnl_usdt ?? 0;

  return (
    <main className="waterfall-shell">
      <header className="waterfall-header">
        <div>
          <p className="eyebrow">CLAUDE CHAMPION · THREE PAPER ACCOUNTS</p>
          <h1>合约瀑布量化监控</h1>
          <p className="subtitle">
            同一套冠军信号 · 20%固定 / 10%固定 / 10%回撤缩仓 · 独立权益路径 · 纸面模拟
          </p>
        </div>
        <div className="header-actions">
          <a className="header-link" href="/waterfall">瀑布页</a>
          <button type="button" onClick={refresh}>刷新</button>
          <span>{updatedAt ? `更新 ${updatedAt.toLocaleTimeString()}` : '等待数据'}</span>
        </div>
      </header>

      {error ? <div className="error-box">瀑布 API 不可用：{error}</div> : null}

      {live?.available ? (
        <section className={`live-execution-strip ${live.safe_halt_reason ? 'live-halted' : ''}`}>
          <div>
            <span className="live-kicker">真实执行隔离账户</span>
            <strong>{live.mode === 'dry_run' ? '只读演练' : live.mode}</strong>
            <em>{live.safe_halt_reason ? `SAFE HALT · ${live.safe_halt_reason}` : '状态正常'}</em>
          </div>
          <div className="live-facts">
            <span>权益 {usdt(live.account?.margin_balance ?? 0)}</span>
            <span>可用 {usdt(live.account?.available_balance ?? 0)}</span>
            <span>持仓 {live.positions.filter((row) => row.status === 'open').length}/{live.max_open_positions}</span>
            <span>{fmt(live.leverage, 0)}x · 单笔≤{usdt(live.max_notional_usdt)}</span>
            <span>仓位 {pct(live.base_margin_fraction, 0)} × {fmt(live.sizing?.factor ?? 1, 2)}</span>
            <span>实现回撤 {pct(live.sizing?.drawdown_pct ?? 0)}</span>
            <span>{live.execution_policy} · {live.service?.status || '待启动'}</span>
          </div>
        </section>
      ) : null}

      <section className="waterfall-config">
        <span>当前策略：{summary?.active_strategy || '-'}</span>
        <span>版本：{String(cfg.variant || '-')}</span>
        <span>扫描：Top {String(cfg.broad_top || '-')} 流动性山寨合约</span>
        <span>触发：{String(cfg.watch_interval || '1m')} 收线确认</span>
        <span>BookDepth增强：{cfg.bookdepth_enhancement_enabled ? '开启（标记）' : '关闭'}</span>
        <span>账户：{String(cfg.account_count || 3)} 套独立资金控制 / 同一信号</span>
        <span>最多持仓：{String(cfg.max_open_positions || '-')}</span>
        <span>类型：{Array.isArray(cfg.enabled_families) ? cfg.enabled_families.join(', ') : '-'}</span>
        <span>Core5：{cfg.core5_enabled ? '开启' : '已停用'}</span>
        <span>实盘下单：{cfg.real_order_enabled ? '已开启' : '关闭'}</span>
      </section>

      {system ? (
        <section className="waterfall-metrics">
          <Metric
            label="币安连接"
            value={system.binance.connected ? '正常' : '断流'}
            tone={system.binance.connected ? 'green' : 'red'}
            sub={system.binance.last_candle_age_sec != null ? `1m延迟 ${system.binance.last_candle_age_sec}s` : '无数据'}
          />
          <Metric
            label="CPU"
            value={`${fmt(system.cpu.percent, 0)}%`}
            tone={system.cpu.percent > 85 ? 'red' : 'cyan'}
            sub={`${system.cpu.cores}核 · 负载 ${fmt(system.cpu.load1, 2)}`}
          />
          <Metric
            label="内存"
            value={`${fmt(system.memory.percent, 0)}%`}
            tone={system.memory.percent > 88 ? 'red' : 'cyan'}
            sub={`${fmt(system.memory.used_mb / 1024, 1)}/${fmt(system.memory.total_mb / 1024, 1)}G · swap ${fmt(system.memory.swap_used_mb, 0)}M`}
          />
          <Metric
            label="磁盘"
            value={`${fmt(system.disk.percent, 0)}%`}
            tone={system.disk.percent > 85 ? 'red' : 'green'}
            sub={`剩 ${fmt(system.disk.free_gb, 1)}G / 共 ${fmt(system.disk.total_gb, 0)}G`}
          />
          <Metric
            label="网络"
            value={`↓${fmt(system.network.rx_kbps, 0)} ↑${fmt(system.network.tx_kbps, 0)} KB/s`}
            tone="neutral"
            sub={`累计 ↓${fmt(system.network.rx_total_gb, 1)}G ↑${fmt(system.network.tx_total_gb, 1)}G`}
          />
          <Metric
            label="K线数据"
            value={`${fmt(system.binance.candles_1m / 10000, 0)}万根`}
            tone="cyan"
            sub={`1m · 跨 ${fmt(system.binance.candle_span_days ?? 0, 1)} 天`}
          />
          <Metric
            label="数据库"
            value={`${fmt(system.data.db_mb, 0)}M`}
            tone="neutral"
            sub="SQLite"
          />
          <Metric
            label="采集数据"
            value={`${fmt(system.data.micro.mb, 1)}M`}
            tone="neutral"
            sub={`${system.data.micro.files}文件 · ${fmt(system.data.micro.span_hours, 1)}h`}
          />
          <Metric
            label="监控进程"
            value={system.monitor?.alive ? `${fmt(system.monitor?.rss_mb ?? 0, 0)}M` : '离线'}
            tone={system.monitor?.alive ? ((system.monitor?.rss_mb ?? 0) > 1100 ? 'red' : 'green') : 'red'}
            sub={system.monitor?.alive
              ? `内存 · ${fmt(system.monitor?.watch_symbols ?? 0, 0)}/${fmt(system.monitor?.universe ?? 0, 0)}币`
              : `${fmt(system.monitor?.age_sec ?? 0, 0)}s无心跳`}
          />
        </section>
      ) : null}

      <section className="waterfall-metrics">
        <Metric label="账户权益" value={usdt(equity)} tone="cyan" sub={`初始 ${usdt(summary?.paper_initial_balance_usdt ?? 0)}`} />
        <Metric label="可用余额" value={usdt(summary?.paper_free_balance_usdt ?? 0)} tone="green" sub={`已用保证金 ${usdt(summary?.paper_used_margin_usdt ?? 0)}`} />
        <Metric label="已实现 PnL" value={usdt(realized)} tone={realized >= 0 ? 'green' : 'red'} />
        <Metric label="未实现 PnL" value={usdt(unrealized)} tone={unrealized >= 0 ? 'green' : 'red'} />
        <Metric label="监控合约" value={summary?.watch ?? 0} tone="cyan" />
        <Metric label="持仓中" value={summary?.open_positions ?? 0} tone="red" />
        <Metric label="已平仓" value={summary?.closed_positions ?? 0} tone="neutral" />
        <Metric label="纸面胜率" value={pct(summary?.win_rate ?? 0)} tone="green" />
      </section>

      {(summary?.accounts?.length ?? 0) > 0 ? (
        <section className="waterfall-grid">
          {(summary?.accounts ?? []).map((acc) => {
            const accEquity = acc.paper_equity_usdt ?? acc.paper_initial_balance_usdt ?? 0;
            const accRealized = acc.paper_realized_pnl_usdt ?? 0;
            const accUnrealized = acc.paper_unrealized_pnl_usdt ?? 0;
            return (
              <Panel title={`独立账户 · ${acc.strategy_label || acc.label || strategyTag(acc.strategy)}`} count={acc.closed_positions} key={acc.account_id}>
                <div className="waterfall-metrics">
                  <Metric label="账户权益" value={usdt(accEquity)} tone="cyan" sub={sizingText(acc)} />
                  <Metric label="已实现 PnL" value={usdt(accRealized)} tone={accRealized >= 0 ? 'green' : 'red'} />
                  <Metric label="未实现 PnL" value={usdt(accUnrealized)} tone={accUnrealized >= 0 ? 'green' : 'red'} />
                  <Metric label="已实现回撤" value={pct(acc.current_drawdown_pct ?? 0)} tone={(acc.current_drawdown_pct ?? 0) >= 0.15 ? 'red' : 'neutral'} sub={`历史最大 ${pct(acc.max_drawdown_pct ?? 0)}`} />
                  <Metric label="持仓中" value={acc.open_positions} tone="red" />
                  <Metric label="已平仓" value={acc.closed_positions} tone="neutral" />
                  <Metric label="胜率" value={pct(acc.win_rate ?? 0)} tone="green" sub={`均值 ${pct(acc.avg_pnl_pct ?? 0)}`} />
                </div>
              </Panel>
            );
          })}
        </section>
      ) : null}

      {live?.available ? (
        <section className="waterfall-grid">
          <Panel title="真实执行仓位" count={live.positions.length}>
            {live.positions.length ? <div className="wf-position-list">
              {live.positions.map((p) => <article className="wf-position-card" key={p.position_id}>
                <div className="wf-card-head"><strong>{p.symbol}</strong><span>{p.status} · {p.protected ? '已保护' : '未保护'}</span></div>
                <div className="wf-card-main"><b>{fmt(p.entry_price, 8)}</b><span>止损 {fmt(p.structure_stop_price, 8)} / 追踪 {fmt(p.trail_price, 8)}</span></div>
                <div className="wf-card-meta"><span>数量 {fmt(p.quantity, 8)}</span><span>强平 {fmt(p.liquidation_price, 8)}</span><span>{date(p.updated_time)}</span></div>
              </article>)}
            </div> : <div className="empty-state">暂无真实仓位</div>}
          </Panel>
          <Panel title="真实订单状态" count={live.orders.length}>
            {live.orders.length ? <div className="wf-signal-list">
              {live.orders.slice(0, 10).map((o) => <article className="wf-signal wf-neutral" key={o.client_order_id}>
                <div><strong>{o.side} {o.symbol}</strong><span>{date(o.updated_time)}</span></div>
                <p>{o.order_type} / {o.execution_policy} / {o.state}</p>
                <p>成交 {fmt(o.filled_quantity, 8)} @ {fmt(o.average_price, 8)}</p>
                <p>信号→首成 {o.signal_to_fill_ms == null ? '--' : `${o.signal_to_fill_ms}ms`} / 完全成交 {o.signal_to_final_fill_ms == null ? '--' : `${o.signal_to_final_fill_ms}ms`}</p>
                <p>总滑点 {fmt(o.slippage_bps, 2)}bp / 到达滑点 {fmt(o.arrival_slippage_bps, 2)}bp</p>
              </article>)}
            </div> : <div className="empty-state">暂无真实订单</div>}
          </Panel>
        </section>
      ) : null}

      <section className="waterfall-grid">
        <Panel title="当前纸面持仓" count={openPositions.length}>
          {openPositions.length ? (
            <div className="wf-position-list">
              {openPositions.map((p) => {
                const mfe = p.best_price > 0 ? p.entry_price / p.best_price - 1 : 0;
                const mae = p.entry_price > 0 ? p.worst_price / p.entry_price - 1 : 0;
                return (
                  <article className="wf-position-card" key={p.position_id}>
                    <div className="wf-card-head">
                      <strong>{p.symbol}</strong>
                      <span>{strategyTag(p.strategy)} · {familyText(p.family)}</span>
                    </div>
                    <div className="wf-card-main">
                      <b>{fmt(p.mark_price ?? p.entry_price, 8)}</b>
                      <span>入场 {fmt(p.entry_price, 8)} / 止损 {fmt(p.stop_price, 8)}</span>
                    </div>
                    <div className="wf-card-meta">
                      <span>保证金 {usdt(p.margin_usdt)}</span>
                      <span>名义 {usdt(p.notional_usdt)}</span>
                      <span>{fmt(p.leverage, 1)}x</span>
                      <span>浮盈 {pct(p.unrealized_pnl_pct ?? 0)}</span>
                      <span>ROI {pct(p.margin_roi_pct ?? 0)}</span>
                      <span>MFE {pct(mfe)}</span>
                      <span>MAE {pct(mae)}</span>
                    </div>
                    <p>{p.rule} / {p.exit_profile}</p>
                  </article>
                );
              })}
            </div>
          ) : <div className="empty-state">暂无纸面持仓</div>}
        </Panel>

        <Panel title="最新交易信号" count={newestSignals.length}>
          <div className="wf-signal-list">
            {newestSignals.length ? newestSignals.map((s) => (
              <article className={`wf-signal ${actionClass(s.action)}`} key={s.signal_id}>
                <div>
                  <strong>{actionText(s.action)} {s.symbol}</strong>
                  <span>{date(s.decision_time)}</span>
                </div>
                <p>【{strategyTag(s.strategy)}】价格 {fmt(s.price, 8)} / 止损 {fmt(s.stop_price, 8)}</p>
                <p>{familyText(s.family)} / {s.rule}</p>
                <p>档位 {tierText(s.tier)} / 保证金 {usdt(s.margin_usdt ?? 0)} / 名义 {usdt(s.notional_usdt ?? 0)}</p>
                {s.action !== 'open_short'
                  ? <b>{pct(s.pnl_pct)} · 权益 {usdt(s.account_equity_usdt ?? 0)}</b>
                  : <b>置信 {fmt(s.confidence, 3)} · {fmt(s.leverage ?? 1, 1)}x</b>}
              </article>
            )) : <div className="empty-state">暂无瀑布信号</div>}
          </div>
        </Panel>
      </section>

      <Panel title="交易明细" count={closedPositions.length}>
        <div className="table-wrap">
          <table className="waterfall-table">
            <thead>
              <tr>
                <th>Symbol</th><th>类型</th><th>入场</th><th>出场</th><th>收益</th><th>保证金ROI</th>
                <th>PnL</th><th>保证金</th><th>名义</th><th>杠杆</th><th>原因</th><th>时间</th>
              </tr>
            </thead>
            <tbody>
              {closedPositions.map((p) => (
                <tr key={p.position_id}>
                  <td><b>{p.symbol}</b></td>
                  <td>{familyText(p.family)}</td>
                  <td>{fmt(p.entry_price, 8)}</td>
                  <td>{fmt(p.exit_price, 8)}</td>
                  <td className={p.pnl_pct >= 0 ? 'trend-up' : 'trend-down'}>{pct(p.pnl_pct)}</td>
                  <td className={Number(p.margin_roi_pct || 0) >= 0 ? 'trend-up' : 'trend-down'}>{pct(p.margin_roi_pct ?? 0)}</td>
                  <td>{usdt(p.pnl_usdt)}</td>
                  <td>{usdt(p.margin_usdt)}</td>
                  <td>{usdt(p.notional_usdt)}</td>
                  <td>{fmt(p.leverage, 1)}x</td>
                  <td>{p.exit_reason}</td>
                  <td>{date(p.exit_time || p.updated_time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="瀑布监控池" count={topWatch.length}>
        <div className="table-wrap">
          <table className="waterfall-table">
            <thead>
              <tr>
                <th>Symbol</th><th>状态</th><th>类型</th><th>现价</th><th>30m</th><th>2h</th><th>4h</th>
                <th>24h</th><th>24h涨幅</th><th>距高点</th><th>30m额</th><th>量比</th><th>主卖</th><th>更新</th>
              </tr>
            </thead>
            <tbody>
              {topWatch.map((r) => (
                <tr key={r.symbol}>
                  <td><b>{r.symbol}</b></td>
                  <td><span className={`badge ${r.status === 'in_position' ? 'badge-active' : ''}`}>{r.status}</span></td>
                  <td>{familyText(r.family)}</td>
                  <td>{fmt(r.last_price, 8)}</td>
                  <td className={r.ret_30m >= 0 ? 'trend-up' : 'trend-down'}>{pct(r.ret_30m)}</td>
                  <td className={r.ret_2h >= 0 ? 'trend-up' : 'trend-down'}>{pct(r.ret_2h)}</td>
                  <td className={r.ret_4h >= 0 ? 'trend-up' : 'trend-down'}>{pct(r.ret_4h)}</td>
                  <td className={r.ret_24h >= 0 ? 'trend-up' : 'trend-down'}>{pct(r.ret_24h)}</td>
                  <td>{pct(r.runup_24h)}</td>
                  <td>{pct(r.dd_from_24h_high)}</td>
                  <td>{fmt(r.qv30 / 1_000_000, 2)}M</td>
                  <td>{fmt(r.volr20, 2)}x</td>
                  <td>{pct(r.tsell)}</td>
                  <td>{date(r.updated_time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="回测与实验记录" count={replays.length}>
        <div className="table-wrap">
          <table className="waterfall-table">
            <thead>
              <tr>
                <th>时间段</th><th>模式</th><th>版本</th><th>类型</th><th>币数</th><th>信号</th><th>交易</th>
                <th>日均</th><th>胜率</th><th>PF</th><th>均收益</th><th>中位</th><th>MAE</th><th>MFE</th><th>3%+</th><th>5%+</th><th>更新</th>
              </tr>
            </thead>
            <tbody>
              {replays.map((r, idx) => (
                <tr key={`${r.updated_time || idx}-${r.variant || ''}-${r.mode || ''}`}>
                  <td>{r.start || '-'} ~ {r.end || '-'}</td>
                  <td>{replayModeText(r)}</td>
                  <td>{r.variant || '-'}</td>
                  <td>{Array.isArray(r.families) ? r.families.join(', ') : '-'}</td>
                  <td>{r.symbols ?? '-'}</td>
                  <td>{r.signals ?? '-'}</td>
                  <td>{replayTrades(r) || '-'}</td>
                  <td>{fmt(r.trades_per_day ?? 0, 2)}</td>
                  <td>{pct(r.win_rate ?? 0)}</td>
                  <td>{r.profit_factor == null ? '-' : fmt(r.profit_factor, 3)}</td>
                  <td className={Number(r.avg_pnl_pct || 0) >= 0 ? 'trend-up' : 'trend-down'}>{pct(r.avg_pnl_pct ?? 0)}</td>
                  <td className={Number(replayMedian(r)) >= 0 ? 'trend-up' : 'trend-down'}>{pct(replayMedian(r))}</td>
                  <td>{pct(r.avg_mae_pct ?? 0)}</td>
                  <td>{pct(r.avg_mfe_pct ?? 0)}</td>
                  <td>{pct(replayBig3(r))}</td>
                  <td>{pct(replayBig5(r))}</td>
                  <td>{date(r.updated_time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </main>
  );
}

function Metric({ label, value, tone, sub }: { label: string; value: ReactNode; tone: string; sub?: ReactNode }) {
  return (
    <div className={`metric-cell tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {sub ? <em className="metric-sub">{sub}</em> : null}
    </div>
  );
}

function Panel({ title, count, children }: { title: string; count: number; children: ReactNode }) {
  return (
    <section className="monitor-section wf-panel">
      <div className="section-heading">
        <h2>{title}</h2>
        <span className="section-count">{count}</span>
      </div>
      {children}
    </section>
  );
}
