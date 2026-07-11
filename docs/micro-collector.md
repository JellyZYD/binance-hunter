# 微观数据采集器 (collect-micro)

采集币安不提供历史归档的三类衍生品微观数据，为策略研究积累底牌：

| 流 | 来源 | 频率 | 用途 |
|---|---|---|---|
| OI | REST /fapi/v1/openInterest，全观察池(450币) | 每 60s | 实时细粒度持仓量（Vision metrics 只有 5m 归档）|
| 爆仓流 | websocket !forceOrder@arr 全市场 | 实时 | 级联判别的最直接信号，币安已下架历史版，只能实时采 |
| 热池盘口 | REST /fapi/v1/depth?limit=20，涨幅榜前30 | 每 30s | 瀑布前买盘塌陷研究（top5 档 + 20 档名义额合计）|

## 存储预算（30G 服务器）

约 25-30 MB/天，parquet 按小时分文件存 `backend/storage/micro/`，
默认 90 天环形保留（启动时+每日自动清理超期文件）→ 峰值占用 < 3 GB。

**拿回本地**：建议每周一次，约 200 MB/周：
```bash
scp -r root@pixia.cc:/opt/binance-hunter/backend/storage/micro E:\2C2G\币安数据库\micro_server\
```

## 本地/服务器启动

```bash
python backend/run.py collect-micro --broad-top 450
```

systemd 单元（服务器，与现有三个服务并列）：

```ini
# /etc/systemd/system/binance-hunter-micro.service
[Unit]
Description=binance-hunter micro data collector
After=network-online.target

[Service]
WorkingDirectory=/opt/binance-hunter
EnvironmentFile=/etc/binance-hunter.env
ExecStart=/usr/bin/python3 backend/run.py collect-micro --broad-top 450
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now binance-hunter-micro
```

已本地实测：75 秒运行三路数据全部正常落盘（2026-07-12）。
