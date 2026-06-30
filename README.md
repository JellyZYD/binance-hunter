# Binance Hunter

币安 USD-M 合约「妖币冲高回落做空猎手」。第一阶段只做选币、监控、报警和可视化，不自动下单。

> 仓库原本是 Pixel Canvas 画板项目，现已整体改造为本项目并按功能模块重新整理目录。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `backend/` | Python 策略后端：REST discovery、WebSocket 监控、SQLite、回测、只读报警 API |
| `frontend/` | Next.js 轻量面板（中文单语言）：流动性榜、活跃妖币池、报警、回测记录 |
| `frontend/src/app/api/hunter/` | Next API 代理：把网页请求转发到 Python 只读 API |
| `database/` | SQLite schema 和数据库说明 |
| `deploy/` | Ubuntu 一键部署、systemd、Nginx、Vercel 说明 |
| `docs/` | 策略和前端说明 |

后端可独立运行，不依赖前端。Binance REST 代理/梯子通过 `HUNTER_NETWORK_PROXY` 环境变量配置，不提交真实代理地址。

## 本地启动

先启动 Python 后端 API：

```bash
cd backend
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell 用 .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py web --host 127.0.0.1 --port 8787
```

另开终端启动网页：

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

访问 `http://localhost:3000`。

## 实盘监控

```bash
cd backend
python run.py monitor --top 120 --broad-top 220 --discover-every 15m --max-workers 8
```

2 核 2G 服务器先用 `top=120 broad-top=220 max-workers=8`，稳定后再提高到 150/200。监控只处理 Binance kline `x=true` 的已收线 K 线，回测和实盘共用同一个 `SignalEngine`。

## 配置

| 环境变量 | 说明 |
| --- | --- |
| `HUNTER_API_BASE_URL` | 前端访问 Python API 的地址，默认 `http://127.0.0.1:8787` |
| `HUNTER_NETWORK_PROXY` | Binance REST 代理/梯子，例如 `http://127.0.0.1:7890` |
| `HUNTER_DB_PATH` | SQLite 路径，默认 `backend/storage/hunter.db` |
| `WECOM_WEBHOOK_URL` | 企业微信报警 webhook，可选 |
| `NEXT_PUBLIC_APP_URL` | 网页公开地址，可选 |

## 验证

```bash
cd backend
python -m unittest discover -s tests
python -m compileall pump_dump_hunter

cd ../frontend
npm run build
```

## 部署

服务器直接执行：

```bash
sudo DOMAIN=your.domain.com bash deploy/setup.sh
```

只更新代码：

```bash
sudo bash deploy/update.sh
```

如果前端部署到 Vercel，服务器只跑 `binance-hunter-monitor` 和 `binance-hunter-api`，然后把 Vercel 的 `HUNTER_API_BASE_URL` 指到服务器公开 API 地址。详见 `deploy/README.md`。
