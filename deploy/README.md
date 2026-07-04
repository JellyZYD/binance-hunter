## Lifecycle Expert Production Version

当前默认生产策略已经升级为生命周期专家版：

- `signals.strategy_version=lifecycle_expert`
- WebSocket 周期：`5m`, `15m`
- 做多：5m lifecycle long combo
- 顶部/做空：15m `fast_top`, `fast_short`, `slow_warning`, `slow_short`
- PumpWatch 同类信号冷却：2h
- 模型目录：`backend/pump_dump_hunter/ml/models/lifecycle/`

服务器执行 `sudo bash deploy/update.sh` 后，检查：

```bash
journalctl -u binance-hunter-monitor -n 80 --no-pager
curl -s http://127.0.0.1:8787/api/model | head
```

`/api/model` 应包含 `lifecycle_ready: true`。如果企业微信配置了 `WECOM_WEBHOOK_URL`，推送内容会带 `interval/mode/state/model/score`。

High-pump production add-on:
- `/api/summary` strategy should show `lifecycle_high_pump_enabled=true`.
- `/api/model` lifecycle runtime should show `high_pump_enabled=true`, `high_pump_min_gain_pct=40`, and `pump_signal_min_gain_pct=25`.
- Broad discovery is a shadow watch pool. Formal PumpWatch, top/short experts, and the main active-contract board only arm after lifecycle max gain reaches 25%; long-signal-derived PumpWatch keeps the 15% flat-long/turn-short maturity rule.
- `high_top` is a 40% pump top / flat-long warning and emits at most once per PumpWatch lifecycle; `short_signal` still requires strict breakdown-style confirmation.

# Deployment

目标服务器：Ubuntu，2 核 2G 可运行。可以选择整站部署在服务器，或后端在服务器、前端放 Vercel。

服务名：

- `binance-hunter-monitor.service`：REST discovery + WebSocket 监控。
- `binance-hunter-api.service`：Python 只读 API，绑定 `127.0.0.1:8787`。
- `binance-hunter-web.service`：Next dashboard，绑定 `127.0.0.1:3000`。
- Nginx：传入 `DOMAIN` 时对外公开 `/`（网站）、`/hunter-api/`（API 代理）、`/health`。

## 一键部署（HTTP）

```bash
sudo DOMAIN=your.domain.com bash deploy/setup.sh
```

脚本自动安装依赖（Node 20 + Python）、clone 仓库到 `/opt/binance-hunter`、启动三个服务并配置 Nginx。

## 整站部署 + 自带证书 HTTPS（推荐）

把已有证书（`SSL_CERT` 用 fullchain，含中间证书）上传到服务器后：

```bash
# 先把证书放到服务器，例如 /etc/ssl/<domain>/
sudo mkdir -p /etc/ssl/pixia
sudo mv /root/www.pixia.cc.pem /root/www.pixia.cc.key /etc/ssl/pixia/
sudo chmod 600 /etc/ssl/pixia/www.pixia.cc.key

# 一行部署：443 + 80 自动跳转 443
curl -fsSL https://raw.githubusercontent.com/JellyZYD/binance-hunter/main/deploy/setup.sh \
  | sudo DOMAIN=pixia.cc SERVER_NAME="pixia.cc www.pixia.cc" \
         SSL_CERT=/etc/ssl/pixia/www.pixia.cc.pem \
         SSL_KEY=/etc/ssl/pixia/www.pixia.cc.key \
         bash
```

相关参数：

| 变量 | 说明 |
| --- | --- |
| `DOMAIN` | 主域名，用于提示信息和默认 server_name |
| `SERVER_NAME` | Nginx `server_name`，多个域名空格分隔，默认取 `DOMAIN` |
| `SSL_CERT` | fullchain 证书路径，提供后启用 HTTPS |
| `SSL_KEY` | 私钥路径 |

> 证书续期后，把新文件覆盖到同名路径再 `sudo systemctl reload nginx` 即可（若 443 改由 Xray 持有，见下方文档，改为 `restart xray`）。

## 只跑后端，前端放 Vercel（可选）

```bash
sudo INSTALL_FRONTEND=0 DOMAIN=api.your.domain.com bash deploy/setup.sh
```

然后在 Vercel：Root Directory 设为 `frontend`，环境变量 `HUNTER_API_BASE_URL=https://api.your.domain.com/hunter-api`。

## 服务器需要代理才能访问 Binance 时（可选）

新加坡/海外服务器一般可直连，无需此项。国内服务器需先在本机跑好代理：

```bash
sudo HUNTER_NETWORK_PROXY=http://127.0.0.1:7890 DOMAIN=your.domain.com bash deploy/setup.sh
```

脚本会写入 `/etc/binance-hunter.env`。后续修改：

```bash
sudo nano /etc/binance-hunter.env
sudo systemctl restart binance-hunter-monitor.service binance-hunter-api.service
```

## 更新代码

```bash
sudo bash deploy/update.sh
```

`update.sh` 只拉取代码 + 重装依赖 + 重启服务，**不改 Nginx 和证书**，所以手动改过的 Nginx（如 Xray 端口复用）不会被它覆盖。注意：`git reset --hard` 会覆盖服务器上对仓库内文件（如 `backend/config/settings.json`）的本地修改——调参请在本地仓库改后提交。

## ML 模型（`signals.mode="ml"`）

- 默认信号走 ML 两段式打分,模型文件随仓库分发(`backend/pump_dump_hunter/ml/models/`),`update.sh` 会一并拉取。
- 服务器只做**推理**,`requirements.txt` 已含 `numpy/pandas/lightgbm`(update.sh 自动装)。**训练只在本地**(2核2G 不训),重训后 push 模型文件即可,详见 [`../docs/ml.md`](../docs/ml.md)。
- 更新后 `journalctl -u binance-hunter-monitor` 应出现 `ML scorer ready=True`;若 `ready=False`(依赖装失败/模型缺失),会不发信号,可临时把 `signals.mode` 改回 `v2` 重启。
- **做多线**(`signals.long_enabled=true`):除信号外,discovery 每 15m 会为做多监管币多拉 4 个 `/futures/data/` 接口(OI/多空/taker),日志 `long flow refreshed M/N`;想关掉做多线把 `long_enabled` 设 `false` 重启即可。
- 若 `pip install lightgbm` 在 2核2G 上因内存失败,先加 swap(见下方)再 `update.sh`。

## 扩大候选池 / 调参

选币分三层（详见 `backend/pump_dump_hunter/discovery.py`）：

1. **扫描池** `--broad-top`：按 24h 成交额取前 N 个币进入扫描。
2. **监控池** `--top`：扫描后按 15m/30m 流动性排序取前 N 个，这些才会被 WebSocket 盯盘，也才有资格成为妖币。
3. **影子妖币池** `backend/config/settings.json` 的 `params.*` 阈值：监控池中满足拉升幅度的进入 PumpWatch 影子观察，用于持续更新锚点和高点。
4. **正式监管池** `signals.lifecycle_pump_signal_min_gain_pct`：生命周期最高涨幅达到 25% 后，才显示在主监控面板，并允许 top/short 专家模型发信号。

改监控规模（`top`/`broad-top`）——编辑 systemd 后重启（survive 重启与 `update.sh`）：

```bash
sudo nano /etc/systemd/system/binance-hunter-monitor.service   # 改 ExecStart 的 --top / --broad-top
sudo systemctl daemon-reload && sudo systemctl restart binance-hunter-monitor
```

放宽妖币门槛（`pump_*_pct`、`min_24h_quote_volume`、`exclude_symbols` 等）——改 `backend/config/settings.json`，**在本地仓库改后提交推送，再在服务器 `update.sh`**（直接改服务器会被 `update.sh` 覆盖）。

## 常用运维命令

```bash
systemctl status binance-hunter-monitor.service binance-hunter-api.service binance-hunter-web.service
journalctl -u binance-hunter-monitor.service -f
sqlite3 /opt/binance-hunter/backend/storage/hunter.db ".tables"
```

## 资源建议（2 核 2G）

- 起步：`HUNTER_TOP=120 HUNTER_BROAD_TOP=220 HUNTER_MAX_WORKERS=8`
- 稳定后可提到 `HUNTER_TOP=200~250 HUNTER_BROAD_TOP=400 HUNTER_MAX_WORKERS=12`
- 前端 `npm run build` 内存吃紧时先加 2G swap（见下方文档）。

## 443 端口复用：网站 + 翻墙（Xray）共存

如果想让同一台服务器、同一个 443 端口，既跑网站又当个人翻墙节点（Xray VLESS + Vision，浏览器流量回落给网站），见 [`https-xray.md`](./https-xray.md)。
