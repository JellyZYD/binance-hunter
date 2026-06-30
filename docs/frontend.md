# Frontend

前端是一个轻量 Next.js 面板（中文单语言），只读展示 Python 后端数据，不直接访问 SQLite。代码位于仓库的 `frontend/` 目录。

## 页面

- `src/app/page.tsx`：主页面，直接渲染 `HunterDashboard`。
- `src/app/layout.tsx`：根布局，`lang="zh-CN"`。
- `src/components/hunter/HunterDashboard.tsx`：数据拉取和表格展示。
- `src/app/api/hunter/[...path]/route.ts`：代理到 Python API。

## API 代理

浏览器访问：

```text
/api/hunter/summary
/api/hunter/liquidity
/api/hunter/pumps
/api/hunter/alerts
/api/hunter/backtests
```

Next route 会转发到：

```text
${HUNTER_API_BASE_URL}/api/*
```

本地默认 `HUNTER_API_BASE_URL=http://127.0.0.1:8787`。

## Vercel

前端可以单独部署到 Vercel。需要注意：

1. Python API 不能只绑定 `127.0.0.1`，必须通过服务器 Nginx 暴露一个 HTTPS 地址。
2. 在 Vercel 环境变量设置 `HUNTER_API_BASE_URL=https://your.domain.com/hunter-api`。
3. 服务器继续只跑 Python monitor/API，把 Next 的 CPU 和内存开销移到 Vercel。

如果不想暴露 Python API，就把 Next 也部署在服务器上，让 `HUNTER_API_BASE_URL=http://127.0.0.1:8787`。
