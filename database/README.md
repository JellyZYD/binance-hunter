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

## 迁移方式

第一版不引入 Alembic/Prisma，避免 2 核 2G 服务器负担。新增列时在 `Store.init_db()` 里通过 `ALTER TABLE` 做轻量补列，旧库启动即可自动兼容。

## 备份

实盘建议定时备份：

```bash
sqlite3 backend/storage/hunter.db ".backup '/var/backups/hunter/hunter-$(date +%F).db'"
```

不要把 `.db` 文件提交到 Git；本地 bb 回测库目前数百 MB，只能作为本机数据源或服务器外部数据卷。
