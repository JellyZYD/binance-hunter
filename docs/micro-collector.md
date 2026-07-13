# 微观数据采集器 (collect-micro)

采集币安不提供历史归档的三类衍生品微观数据，为策略研究积累底牌：

| 流 | 来源 | 频率 | 用途 |
|---|---|---|---|
| OI | REST /fapi/v1/openInterest，全观察池(450币) | 每 60s | 实时细粒度持仓量（Vision metrics 只有 5m 归档）|
| 爆仓流 | websocket !forceOrder@arr 全市场 | 实时 | 级联判别的最直接信号，币安已下架历史版，只能实时采 |
| 热池盘口 | REST /fapi/v1/depth?limit=20，涨幅/跌幅/成交额平衡池 60 个 | 每 30s | top5 档 + 20 档名义额合计，并为 core5 的 BookDepth 增强档提供实时代理 |

## 存储预算（30G 服务器）

约 25-30 MB/天，parquet 按小时分文件存 `backend/storage/micro/`，
默认 90 天环形保留（启动时+每日自动清理超期文件）→ 峰值占用 < 3 GB。
写入是**缓冲的**：每 300 秒 flush 一次，所以启动后 **约 5 分钟才出现第一批文件**，`ls` 早了会看到空目录，属正常。

**拿回本地**：建议每周一次，约 200 MB/周：
```bash
scp -r root@pixia.cc:/opt/binance-hunter/backend/storage/micro E:\2C2G\币安数据库\micro_server\
```

## 服务器启动

```bash
cat > /etc/systemd/system/binance-hunter-micro.service <<'EOF'
[Unit]
Description=binance-hunter micro data collector
After=network-online.target

[Service]
WorkingDirectory=/opt/binance-hunter
EnvironmentFile=/etc/binance-hunter.env
ExecStart=/opt/binance-hunter/backend/.venv/bin/python backend/run.py collect-micro --broad-top 450 --depth-top 60 --depth-interval 30
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now binance-hunter-micro
```

> ExecStart 必须用 venv 里的 python（`backend/.venv/bin/python`），系统 `python3` 没装依赖。

验收（等 ~5 分钟后）：
```bash
systemctl status binance-hunter-micro --no-pager | head -3
ls -lh /opt/binance-hunter/backend/storage/micro/   # 应有 oi_/liq_/depth_ 三类 parquet
cat /opt/binance-hunter/backend/storage/micro/latest_depth.json | head -c 300
```

`latest_depth.json` 每轮盘口抓取后原子更新，不等 5 分钟 parquet flush。监控进程读取它并在盘口增量通过时，将同一个 Codex core5 纸面信号标记为 `bookdepth_strong`（前端/企业微信显示“BookDepth增强”）。微观服务重启时盘口轮询会先错峰等待 120 秒，再用约两分钟形成基线，因此首次增强档约需 4-5 分钟；期间 core5+agg 继续正常运行。

## 限流保护（2026-07-12 加固）

- OI/盘口两个 REST 轮询遇到 **418/429 会退避 200 秒**再重扫，不会在封禁期间继续轰炸（否则会延长封禁）。
- 三路轮询**错峰启动**：OI 延后 90s、盘口延后 120s，避免与 monitor 开机预热同时冲击币安权重；爆仓流 websocket 不占 REST 权重，立即启动。
- **与 monitor 同机共享 IP 权重**：重启大量服务时注意别让采集器和 monitor 全量预热同时发生（见 `deploy/README.md` 的"限流与重启"一节）。

写文件格式：优先 parquet（需 pyarrow，已在 requirements）；**缺 parquet 引擎时自动降级 `*.csv.gz`**，采集器不会因缺依赖而崩溃循环（2026-07-12 踩过：venv 无 pyarrow，to_parquet 每 5min 崩一次）。

已本地实测：75 秒三路正常落盘；服务器 2026-07-12 上线，DB-first 预热 + 错峰后无封禁。
