# Database

项目使用 SQLite，默认路径是 `backend/storage/hunter.db`。数据库由 `backend/pump_dump_hunter/data/store.py` 自动创建，`database/schema.sql` 是给人和后续 agent 阅读的结构说明。

## 表

| 表 | 说明 |
| --- | --- |
| `candles` | 已收线 K 线，按 `symbol + interval + open_time` 去重 |
| `liquidity_snapshots` | 每次 discovery 的 TopN 流动性和涨幅快照，含 15m/30m/4h/12h/1d |
| `pump_events` | 入池妖币事件，高点、锚点、过期时间、证据 |
| `watchlist` | 当前 WebSocket 应盯盘的 symbol 摘要 |
| `alerts` | `early_alert` 和 `short_signal` 报警 |
| `backtest_runs` | 回测参数和指标归档 |
| `waterfall_watch` | 当前 1m 瀑布监控特征和最新价格 |
| `waterfall_positions` | 两套瀑布策略的纸面持仓、退出结果、保证金和盈亏 |
| `waterfall_signals` | 瀑布开空、止盈、止损和超时退出信号 |
| `waterfall_shadow_micro` | 可选的 aggTrade/bookTicker 原始影子事件 |

## 双策略状态隔离

`waterfall_positions.strategy` 和 `waterfall_signals.strategy` 是恢复状态与
账户统计的强制边界。当前两个生产策略 ID：

- `waterfall_core5_agg_1m`
- `claude_board_wf_1m`

监控进程重启时，各引擎只读取自己的活跃持仓和完整历史持仓。完整历史用于
恢复累计已实现盈亏；不能重新加回 `LIMIT 1000`，否则长期运行后早期盈亏会
从纸面账户中消失。API 顶部总账户由两个独立账户相加，不是给全局查询硬加
一份 100U 初始资金。

## 迁移方式

第一版不引入 Alembic/Prisma，避免 2 核 2G 服务器负担。新增列时在 `Store.init_db()` 里通过 `ALTER TABLE` 做轻量补列，旧库启动即可自动兼容。

## 备份

实盘建议定时备份：

```bash
sqlite3 backend/storage/hunter.db ".backup '/var/backups/hunter/hunter-$(date +%F).db'"
```

不要把 `.db` 文件提交到 Git；本地 bb 回测库目前数百 MB，只能作为本机数据源或服务器外部数据卷。
