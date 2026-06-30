# Hunter Backend

Python 后端负责策略、数据、监控、回测、SQLite 写入和只读 API。它可以独立运行，不依赖 Next.js。

## 核心模块

| 路径 | 作用 |
| --- | --- |
| `pump_dump_hunter/discovery.py` | REST 扫描流动性 TopN、15m/30m/4h/12h/1d 涨幅、量比和入池条件 |
| `pump_dump_hunter/engine/signal_engine.py` | 事件驱动信号引擎，live/backtest 共用 |
| `pump_dump_hunter/live.py` | REST discovery + WebSocket 已收线 K 线监控 |
| `pump_dump_hunter/backtest.py` | SQLite 历史数据 replay 回测和参数优化 |
| `pump_dump_hunter/data/store.py` | SQLite schema、读写、旧库补列 |
| `pump_dump_hunter/web.py` | 只读 HTTP API，供 Next 面板读取 |
| `config/settings.json` | 实盘默认配置 |
| `config/bb_settings.json` | 使用本地 bb 数据回测时的配置 |

## 常用命令

```bash
python run.py discover --top 120 --broad-top 220 --max-workers 8
python run.py monitor --top 120 --broad-top 220 --discover-every 15m --max-workers 8
python run.py web --host 127.0.0.1 --port 8787
python run.py status --limit 20
python run.py backfill --days 30 --broad-top 220 --intervals 1m,15m
python run.py backtest --days 30 --top 120 --details
python run.py optimize --days 30 --top 120 --param-grid config/param_grid.json
python run.py replay-alert --alert-id ALERT_ID
```

## 选币入池

默认排除 BTC/ETH/BNB/SOL/XRP/DOGE/ADA/TRX 和股票/贵金属类合约。流程：

1. 24h ticker 缩小 broad universe。
2. 对 broad universe 拉最近 1m K 线和 15m context。
3. 计算 15m/30m 流动性、涨跌幅、振幅、量比，以及 4h/12h/1d 大级别涨幅。
4. 符合短周期强拉或大周期妖币涨幅后进入 pump event。
5. 入池后默认活跃 36 小时；刷新新高会重新布防并延长监控。

## 报警信号

当前只做做空提示：

- `early_alert`：15m 高位横盘 3 根左右后出现向上插针回落或两根 K 线跨周期冲高回落。
- `short_signal`：15m 放量破位，跌破 EMA21 或 15m 实体结构低点，且距离起涨锚点仍有足够空间。

策略默认只使用已收线 K 线，不用未完成 K 线生成正式信号。

## 代理/梯子

REST 代理优先级：

1. `HUNTER_NETWORK_PROXY`
2. `HTTPS_PROXY`
3. `HTTP_PROXY`
4. `config/settings.json` 里的 `network.proxy`

例子：

```bash
HUNTER_NETWORK_PROXY=http://127.0.0.1:7890 python run.py discover --top 120
```

## 数据文件

运行数据默认写到：

- `storage/hunter.db`
- `alerts/YYYY-MM-DD.jsonl`
- `alerts/YYYY-MM-DD.md`
- `reports/*`

这些运行文件默认不进 Git，只保留 `.gitkeep`。
