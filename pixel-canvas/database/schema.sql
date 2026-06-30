CREATE TABLE IF NOT EXISTS candles (
  symbol TEXT NOT NULL,
  interval TEXT NOT NULL,
  open_time INTEGER NOT NULL,
  close_time INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  quote_volume REAL NOT NULL,
  trades INTEGER NOT NULL,
  taker_buy_base REAL NOT NULL,
  taker_buy_quote REAL NOT NULL,
  PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS liquidity_snapshots (
  run_id TEXT NOT NULL,
  snapshot_time INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  rank INTEGER NOT NULL,
  last_price REAL NOT NULL,
  quote_volume_15m REAL NOT NULL,
  quote_volume_30m REAL NOT NULL,
  pct_15m REAL NOT NULL,
  pct_30m REAL NOT NULL,
  amp_15m REAL NOT NULL,
  amp_30m REAL NOT NULL,
  volume_ratio_15m REAL NOT NULL,
  volume_ratio_30m REAL NOT NULL,
  gain_rank_15m INTEGER NOT NULL,
  gain_rank_30m INTEGER NOT NULL,
  selected INTEGER NOT NULL,
  pump_qualified INTEGER NOT NULL,
  data_cutoff_time INTEGER NOT NULL,
  pct_4h REAL NOT NULL DEFAULT 0,
  pct_12h REAL NOT NULL DEFAULT 0,
  pct_1d REAL NOT NULL DEFAULT 0,
  quote_volume_4h REAL NOT NULL DEFAULT 0,
  quote_volume_12h REAL NOT NULL DEFAULT 0,
  quote_volume_1d REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, symbol)
);

CREATE TABLE IF NOT EXISTS pump_events (
  event_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  trigger_window TEXT NOT NULL,
  anchor_price REAL NOT NULL,
  high_price REAL NOT NULL,
  high_time INTEGER NOT NULL,
  current_price REAL NOT NULL,
  max_gain_pct REAL NOT NULL,
  status TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  early_alerted_after_high_time INTEGER,
  short_alerted_after_high_time INTEGER
);

CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  status TEXT NOT NULL,
  high_price REAL NOT NULL,
  high_time INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  last_update_time INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  level TEXT NOT NULL,
  decision_time INTEGER NOT NULL,
  source_candle_close_time INTEGER NOT NULL,
  data_cutoff_time INTEGER NOT NULL,
  price REAL NOT NULL,
  invalidation_price REAL NOT NULL,
  anchor_price REAL NOT NULL,
  high_price REAL NOT NULL,
  remaining_downside_pct REAL NOT NULL,
  volume_ratio REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  risks_json TEXT NOT NULL,
  pushed INTEGER NOT NULL DEFAULT 0,
  push_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  days INTEGER NOT NULL,
  params_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  train_start INTEGER,
  train_end INTEGER,
  validation_start INTEGER,
  validation_end INTEGER
);
