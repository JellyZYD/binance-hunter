## Claude 冠军策略三账户生产版（当前默认）

默认监控策略是 `runtime.active_strategy = "claude_board_wf_1m"`。一套 Claude 冠军信号驱动三套独立 100U 纸面账户，Core5 已停用且不会实例化：

| 账户 | 保证金控制 | 杠杆 |
|---|---|---:|
| `claude_fixed20` | 固定权益 20% | 10x |
| `claude_fixed10` | 固定权益 10% | 10x |
| `claude_drawdown10` | 10% 基础，按已实现回撤缩到 7.5%/5%/2.5% | 10x |

- 触发：24h 涨幅≥40% + 距 60m 高点跌≥7% + 60m 成交额≥30万U，等 1m 收线。
- 出场：B 结构止损 + 3.5%/3% 追踪 + 10 根主动卖盘延迟止盈 + 4h 超时。
- 企微每个主信号只推一次，正文同时列出三账户变化。
- 三账户启动时从 2026-07-13 07:37 CST 的 Claude 主交易历史幂等重放。
- 实盘安全：`WaterfallExecutionAdapter` 默认 paper，`real_order_enabled=false`；改 live 会启动即抛异常拒绝下单。

部署后检查：
```bash
journalctl -u binance-hunter-monitor -n 20 --no-pager   # 应有 websocket connected streams=... 无 418
curl -s https://pixia.cc/api/hunter/waterfall/summary | python3 -m json.tool | grep -A6 strategy_label
```
`accounts` 应只含 `claude_fixed20`、`claude_fixed10`、`claude_drawdown10`，合计初始 300U。

重启只恢复 Claude 主仓位，三套资金账本由主历史重放。旧 Core5 数据保留审计但不参与运行。若币池 REST 请求被
418/429 拒绝，预热会切到严格 DB-only，不再继续逐币请求 K 线。

### 限流与重启（2026-07-12 血泪教训，务必读）

**病根**：monitor 每次启动要 REST 预热 ~400 币的 1m K 线，接近币安权重上限。**连续重启**（或重启时采集器同时全量请求）会叠加超限 → IP 被 418 封禁 → 旧代码把限流当致命错误崩溃 → systemd 5 秒重启 → 再预热 → 封禁续期，形成自我 DDoS 死循环（曾连崩 14 次）。

**已修复的三道防线**（3cf48e3 / a874db4 / 439d875）：
1. **DB-first + 权重限速预热**：预热先读本地 SQLite，只从 REST 补停机缺口/薄币回填（缺口必须补，websocket 补不了停机期）；且所有 klines 调用走**共享权重限速器**（`rest_weight_per_sec=20` ≈ 1200 权重/分钟），即使 15 分钟刷新 383 个币也稳在币安 2400/分钟上限内、给采集器（~570/分钟同 IP）留余量。**根因**：websocket 的 1m K 线零权重、永不封禁；封禁全来自 REST 预热突发。
2. **币池 REST 兜底**：连币池列表（ticker/exchangeInfo）都被封时，回退到数据库已知的币，监控用 websocket+DB 直接跑起来，不再卡在重试循环等解封。
3. **限流自愈**：启动 discovery 遇错退避 180s 重试而非崩溃；采集器 OI/盘口遇 418/429 退避 200s。封禁自然过期后自动恢复，**无需人工干预**。
4. **采集器错峰**：OI/盘口轮询延后 90s/120s 启动，避开 monitor 预热窗口。
5. **watch 池回填**：薄历史币（新上市/轮入轮出）在预热时限速补齐到完整窗口 → 监控合约数从卡住的 ~245 爬到全量 ~383（之前是补历史撞封禁、补一半就断）。

数据库并发：`store.connect` 启用 **WAL 模式 + busy_timeout=8s**，只读 API 进程不再被监控写 K 线锁住（消除 `database is locked` / 网页断联）。

**运维守则**：
- 更新代码走轻量三连即可，**不要连续 restart**：
  ```bash
  cd /opt/binance-hunter && git fetch origin && git reset --hard origin/main
  systemctl restart binance-hunter-monitor          # 涉及前端才用 sudo bash deploy/update.sh
  ```
- 若同时重启 monitor 和采集器，中间隔 60s：`systemctl restart binance-hunter-monitor && sleep 60 && systemctl start binance-hunter-micro`。
- 看是否被封 / 封到几点：
  ```bash
  journalctl -u binance-hunter-monitor -n 20 --no-pager | grep -oP 'banned until \K[0-9]+' | tail -1 \
    | xargs -I{} sh -c 'date -d @$(({} / 1000)) "+封禁到: %F %H:%M:%S %Z"'
  ```
  时间戳不变=在倒计时（好）；变大=还有进程在撞墙，先 `systemctl stop binance-hunter-micro`。封禁期间**只等不重启**。

微观数据采集器（OI/爆仓流/盘口）的完整说明见 [`../docs/micro-collector.md`](../docs/micro-collector.md)。

### 内存与防死机（2026-07-12 加固，2G 机器必读）

这台 2G 机器曾因内存爆连环假死（前端 npm 构建 OOM、双引擎 K 线翻倍、预热字典炸弹）。已建多道防线，正常运行监控进程内存 ~250-300MB（含 pandas/numpy 导入基线 ~250MB）：

1. **K 线内存降 90%**：①`Candle` 用 `@dataclass(slots=True)`（600→136 字节/对象，slots 不改任何逻辑、属性访问略快）；②Claude/Codex 两引擎**共享同一份 K 线字典**（board 引擎不再存第二份）。合计监控进程 K 线内存 ~700MB→~80MB。
1b. **预热字典去重**：`prime_candles` 原本给每个币每根 K 线都生成一条 watch 行（一个币 1500 根≈1260 条），366 币累积 ~46 万条字典再一次性写库——曾把 RSS 顶到 894MB 并造成巨型写锁。改成每个币只留最终一条 → 峰值 RSS 回落到 ~267MB。
2. **systemd 内存硬限（cgroup 兜底）**：超限只重启该服务、**绝不触发系统级 OOM 拖垮 sshd**。一次性配置：
   ```bash
   mkdir -p /etc/systemd/system/binance-hunter-monitor.service.d
   printf '[Service]\nMemoryHigh=850M\nMemoryMax=1000M\n' > /etc/systemd/system/binance-hunter-monitor.service.d/limit.conf
   mkdir -p /etc/systemd/system/binance-hunter-micro.service.d
   printf '[Service]\nMemoryMax=350M\n' > /etc/systemd/system/binance-hunter-micro.service.d/limit.conf
   systemctl daemon-reload && systemctl restart binance-hunter-monitor binance-hunter-micro
   ```
3. **崩溃保护 + 执行优先**：事件循环 per-event try/except（单币坏数据/慢 webhook 不再崩全局）；纸面持仓先落库、企微推送 best-effort 在后（慢/失败推送不丢单）。

**监控进程健康自报**：监控进程每 ~30s 把 RSS/事件数/持仓/币数写 `storage/monitor_health.json`，`/api/system` 读出，`/waterfall` 页"监控进程"卡显示监控 RSS + 存活。`waterfall_quant.rss_soft_limit_mb`（默认 1100）超限日志告警。

## 前端部署：Vercel（推荐，2G 机器免构建）

2G 机器构建 Next.js 易 OOM。前端已可完全托管在 Vercel，服务器只跑后端：

1. Vercel → Add New Project → Import `JellyZYD/binance-hunter`。
2. **Root Directory 设为 `frontend`**（前端在子目录）。
3. 环境变量：`HUNTER_API_BASE_URL = https://pixia.cc/hunter-api`（Nginx 已把 `/hunter-api/` 代理到后端根，route.ts 通配转发，零代码改动）。
4. Deploy。之后 `git push` 到 main，Vercel 自动重新构建部署。

服务器侧：`systemctl stop binance-hunter-web && systemctl disable binance-hunter-web`（停本地前端省内存）。代价：`pixia.cc/` 首页 502（`location /` 指向已停的本地前端，预期），改用 Vercel 网址；`pixia.cc/hunter-api/` 照常给 Vercel 供数据。**数据经 /hunter-api/ 公开无鉴权**（纸面/行情，不敏感；需要时可加 token）。

用 Vercel 后通过标准更新脚本升级，但跳过服务器上的 Next.js 构建：
```bash
cd /opt/binance-hunter
sudo env INSTALL_FRONTEND=0 bash deploy/update.sh
```

该命令仍会重启 `monitor`、`api` 和 `micro`，并执行生产策略校验；Vercel
在 `main` 更新后会独立自动构建前端。

---

## Lifecycle Expert Production Version（历史，已非默认）

以下为上一代生命周期专家版记录；当前 `active_strategy` 是 `claude_board_wf_1m`（见上）。生命周期版：

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
# 四个服务状态（含采集器）
systemctl status binance-hunter-monitor binance-hunter-api binance-hunter-web binance-hunter-micro --no-pager | grep -E "●|Active"

# monitor 心跳（约 30s 一条 waterfall events=... open_positions=...；数字在涨=正常）
journalctl -u binance-hunter-monitor.service -f

# 双策略账户（各 100U，从开机即显示）
curl -s https://pixia.cc/api/hunter/waterfall/summary | python3 -m json.tool | grep -A6 strategy_label

# 系统 + 监控进程健康（CPU/内存/磁盘/网络/币安连接/监控RSS，一站看全）
curl -s http://127.0.0.1:8787/api/system | python3 -m json.tool

# 监控进程内存（防死机核心指标，正常 150-250MB）
grep VmRSS /proc/$(pgrep -f 'run.py monitor')/status

# 采集器三路数据（启动 ~5min 后才有文件）
ls -lh /opt/binance-hunter/backend/storage/micro/
```

> 心跳日志频率由 `websocket.heartbeat_events` 控制（当前 7500 ≈ 30s 一条）。
> API 内网端口 `127.0.0.1:8787`（`/api/hunter/...` 经 Nginx 代理对外）。

## 资源建议（2 核 2G）

- 起步：`HUNTER_TOP=120 HUNTER_BROAD_TOP=220 HUNTER_MAX_WORKERS=8`
- 稳定后可提到 `HUNTER_TOP=200~250 HUNTER_BROAD_TOP=400 HUNTER_MAX_WORKERS=12`
- 瀑布预热并发 `waterfall_quant.max_workers` 已降到 3（防限流，见"限流与重启"）。

### 加 2G swap（前端构建 / lightgbm 安装内存吃紧时）

2G 内存机器跑 `npm run build`（尤其 codex 改动前端后缓存失效那次）会 OOM，把 sshd 一起拖死。**先加一次性 swap 再部署**，一劳永逸：

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab   # 开机自动挂载
```

若已 OOM 假死连不上 SSH：阿里云控制台**重启实例**（本机无需抢救的状态，重启零损失），起来后先加 swap 再重跑部署。

## 443 端口复用：网站 + 翻墙（Xray）共存

如果想让同一台服务器、同一个 443 端口，既跑网站又当个人翻墙节点（Xray VLESS + Vision，浏览器流量回落给网站），见 [`https-xray.md`](./https-xray.md)。
