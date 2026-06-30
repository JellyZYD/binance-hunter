# Deployment

目标服务器：Ubuntu，2 核 2G 可运行。默认把 CPU/内存留给 Python 监控和代理/VPN，网页可以选择部署在服务器或 Vercel。

## 一键部署

```bash
sudo DOMAIN=your.domain.com bash deploy/setup.sh
```

默认安装并启动：

- `binance-hunter-monitor.service`：REST discovery + WebSocket 监控。
- `binance-hunter-api.service`：Python 只读 API，绑定 `127.0.0.1:8787`。
- `binance-hunter-web.service`：Next dashboard，绑定 `127.0.0.1:3000`。
- Nginx：如果传入 `DOMAIN`，公开 `/` 和 `/hunter-api/`。

## 只跑后端，前端放 Vercel

```bash
sudo INSTALL_FRONTEND=0 DOMAIN=api.your.domain.com bash deploy/setup.sh
```

然后在 Vercel 设置：

```text
HUNTER_API_BASE_URL=http://api.your.domain.com/hunter-api
```

如果你给 Nginx 配了 HTTPS，就改成 `https://api.your.domain.com/hunter-api`。

## 代理/梯子

服务器需要走本机代理访问 Binance REST 时：

```bash
sudo HUNTER_NETWORK_PROXY=http://127.0.0.1:7890 DOMAIN=your.domain.com bash deploy/setup.sh
```

脚本会写入 `/etc/binance-hunter.env`。后续修改代理：

```bash
sudo nano /etc/binance-hunter.env
sudo systemctl restart binance-hunter-monitor.service binance-hunter-api.service
```

## 更新

```bash
sudo bash deploy/update.sh
```

## 常用运维命令

```bash
systemctl status binance-hunter-monitor.service
systemctl status binance-hunter-api.service
systemctl status binance-hunter-web.service
journalctl -u binance-hunter-monitor.service -f
journalctl -u binance-hunter-api.service -f
sqlite3 /opt/binance-hunter/backend/storage/hunter.db ".tables"
```

## 资源建议

- 2 核 2G：`HUNTER_TOP=120 HUNTER_BROAD_TOP=220 HUNTER_MAX_WORKERS=8`
- 稳定后：`HUNTER_TOP=150` 或 `200`
- 不建议一开始 `broad_top=400`，REST discovery 会占用更久网络和 CPU。
