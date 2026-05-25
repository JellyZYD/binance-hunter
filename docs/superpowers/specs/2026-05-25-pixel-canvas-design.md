# 协作像素画布 - 设计规格

> 类似 Reddit r/place 的协作像素画布，用户挂机看广告攒点数，合力绘制巨型像素画。

## 1. 项目概述

### 目标
创建一个 2000×2000 的在线协作像素画布，用户通过挂机浏览页面获取点数，每 5 分钟积累 1 点用于放置像素。支持实时聊天、像素作者追踪、后台管理。

### 目标用户
年轻人/学生群体，社交传播性强。

### 成功标准
- 支持 500+ 并发用户流畅操作
- 像素放置延迟 < 200ms
- 聊天消息实时送达
- 广告位自然嵌入，不影响核心体验

## 2. 核心功能

### 2.1 画布系统

**画布规格：**
- 尺寸：2000×2000 像素
- 渲染：HTML5 Canvas API
- 交互：支持缩放（滚轮/双指）、平移（拖拽）、点击放置像素

**像素数据结构：**
```typescript
interface Pixel {
  x: number;          // 0-1999
  y: number;          // 0-1999
  color: string;      // hex color
  userId: string;     // 用户 ID
  nickname: string;   // 用户昵称
  timestamp: number;  // 放置时间戳
}
```

**颜色系统：**
- 预设 32 色调色板（经典 r/place 配色）
- 支持自定义颜色（HEX 输入）

**像素信息查看：**
- 点击已有像素弹出浮窗：显示作者昵称、放置时间
- 游客显示随机昵称，登录用户显示绑定昵称

### 2.2 点数系统

**获取规则：**
- 用户在页面停留每 5 分钟获得 1 点
- 点数上限：最多积攒 12 点
- 前端定时器计时，后端校验

**消耗规则：**
- 每放置 1 个像素消耗 1 点
- 点数不足时按钮置灰，显示倒计时

**防作弊：**
- 前端每 30 秒向后端发送心跳，后端验证活跃状态
- 页面失焦时暂停计时
- 后端记录最后活跃时间，防止伪造

### 2.3 实时聊天室

**功能：**
- 右侧面板，可收起/展开
- 实时消息推送（WebSocket）
- 显示在线人数
- 消息格式：昵称 + 内容 + 时间
- 消息长度限制：200 字符
- 历史消息：加载最近 50 条

**防刷屏：**
- 每 5 秒最多发 1 条消息
- 连续相同消息过滤

### 2.4 用户系统

**匿名游客：**
- 首次访问自动生成 UUID 存入 localStorage
- 随机生成昵称（如 "画师_7823"）
- 立即可用，无需注册

**可选登录：**
- 支持 GitHub OAuth 登录
- 登录后绑定历史像素贡献
- 显示个人统计：总像素数、首次画布时间

### 2.5 多语言支持

**语言切换：**
- 右上角语言切换按钮，显示当前语言国旗/名称
- 点击展开下拉菜单，选择语言后即时切换，无需刷新
- 用户选择存入 localStorage，下次访问自动应用

**支持语言（12 种）：**

| 语言 | 代码 | 说明 |
|------|------|------|
| 中文简体 | zh-CN | 默认语言 |
| 中文繁體 | zh-TW | 港澳台用户 |
| English | en | 英语 |
| 日本語 | ja | 日语 |
| 한국어 | ko | 韩语 |
| Français | fr | 法语 |
| Deutsch | de | 德语 |
| Español | es | 西班牙语 |
| Português | pt | 葡萄牙语 |
| Русский | ru | 俄语 |
| العربية | ar | 阿拉伯语（RTL 支持） |
| हिन्दी | hi | 印地语 |

**实现方案：**
- 使用 `next-intl` 库（Next.js 国际化方案）
- 所有 UI 文案抽取为翻译文件 `/messages/{locale}.json`
- 画布内嵌文字（如昵称）不受语言切换影响
- 聊天消息保持原文显示

### 2.6 后台管理

**管理功能：**
- 画布重置：手动触发或设置定时重置
- 在线人数监控
- 像素回滚：撤销指定区域的最近 N 次操作
- 用户封禁：封禁恶意用户 ID
- 广告配置：管理广告位代码

**访问方式：**
- `/admin` 路径，密码保护（环境变量配置）

## 3. 广告位设计

| 位置 | 类型 | 尺寸 | 说明 |
|------|------|------|------|
| 画布顶部 | 横幅广告 | 728×90 | 页面主要广告位 |
| 聊天室下方 | 矩形广告 | 300×250 | 挂机时持续可见 |
| 点数面板旁 | 小横幅 | 320×50 | 移动端友好 |
| 画布重置页 | 插屏广告 | 全屏 | 重置倒计时时展示 |

## 4. 技术架构

### 4.1 前端

**技术栈：**
- Next.js 14 (App Router)
- Tailwind CSS
- next-intl（多语言国际化）
- Canvas API（画布渲染）
- Socket.io Client（WebSocket）

**页面结构：**
```
/[locale]            — 主页面（画布 + 聊天 + 点数面板）
/[locale]/admin      — 后台管理
```
- URL 带语言前缀：`/zh-CN`、`/en`、`/ja` 等
- 访问 `/` 自动根据浏览器语言或 localStorage 重定向

**画布渲染优化：**
- 离屏 Canvas 缓存，只重绘变化区域
- 视口裁剪：只渲染可见区域的像素
- 像素变化批量更新（16ms 一帧）

### 4.2 后端

**技术栈：**
- Next.js API Routes（REST API）
- Socket.io Server（WebSocket）
- PostgreSQL（持久化存储）
- Redis（像素缓存 + 在线状态 + 点数）

**API 设计：**

```
GET    /api/canvas              — 获取画布数据（分块加载）
POST   /api/pixel               — 放置像素
GET    /api/pixel/:x/:y         — 查询单个像素信息
GET    /api/stats               — 在线人数、总像素数
POST   /api/auth/github         — GitHub OAuth 登录
GET    /api/user/me             — 当前用户信息
POST   /api/admin/reset         — 重置画布（需认证）
POST   /api/admin/rollback      — 回滚操作（需认证）
```

**WebSocket 事件：**
```
pixel:update       — 像素变更广播
chat:message       — 聊天消息广播
user:count         — 在线人数更新
points:update      — 点数更新（私有）
```

### 4.3 数据存储

**Redis 数据结构：**
```
canvas:pixels          — Hash: "x,y" → color（画布快照）
canvas:history         — List: 最近 1000 条像素操作
user:points:{uid}      — String: 用户点数
user:heartbeat:{uid}   — String: 最后活跃时间
online:users           — Set: 在线用户 ID
chat:messages          — List: 最近 50 条聊天
```

**PostgreSQL 表：**
```sql
users (
  id UUID PRIMARY KEY,
  nickname VARCHAR(50),
  github_id VARCHAR(50) UNIQUE,
  total_pixels INT DEFAULT 0,
  created_at TIMESTAMP
)

pixel_history (
  id SERIAL PRIMARY KEY,
  x INT, y INT,
  color VARCHAR(7),
  user_id UUID REFERENCES users(id),
  created_at TIMESTAMP
)

canvas_config (
  key VARCHAR(50) PRIMARY KEY,
  value TEXT
)
```

### 4.4 部署架构

```
用户浏览器
    ↓
Next.js (Vercel) — 静态页面 + API Routes
    ↓
WebSocket 服务器 (新加坡 2核2G)
    ├── Socket.io Server (端口 3001)
    ├── PostgreSQL
    └── Redis
```

- 前端静态资源部署到 Vercel（CDN 加速）
- WebSocket + 后端服务部署到新加坡服务器
- PostgreSQL + Redis 同机部署

## 5. 性能与扩展

**初期目标（2核2G）：**
- 500-1000 并发 WebSocket 连接
- 像素放置延迟 < 200ms
- 聊天延迟 < 100ms

**扩展路径：**
- 流量增长：WebSocket 服务器独立部署，前端走 Vercel
- 数据增长：Redis 像素数据定期归档到 PostgreSQL
- 全球用户：增加 CDN 节点 + WebSocket 就近接入

## 6. 安全考虑

- WebSocket 连接速率限制
- 像素放置频率限制（后端强制 5 分钟间隔）
- 聊天消息 XSS 过滤
- 管理后台密码保护 + IP 白名单
- 环境变量管理敏感配置
