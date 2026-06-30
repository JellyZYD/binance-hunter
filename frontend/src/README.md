# Frontend Source

这是项目的轻量 Next.js 面板，不承担策略计算，只展示后端 API 的只读结果。

## 入口

- `app/page.tsx`：渲染 `HunterDashboard`。
- `components/hunter/HunterDashboard.tsx`：刷新数据、展示表格。
- `app/api/hunter/[...path]/route.ts`：代理到 Python API。
- `app/globals.css`：纯 CSS 样式，无 Tailwind runtime 依赖。

## 数据流

```text
Browser -> Next /api/hunter/* -> Python backend /api/* -> SQLite
```

前端不直接读数据库，也不写策略状态。这样部署到 Vercel 时只需要配置 `HUNTER_API_BASE_URL`。
