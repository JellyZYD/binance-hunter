# Pixel Canvas

协作像素画布 — 一个类似 Reddit r/place 的多人在线像素绘画平台。

## 功能

- **多人协作** — 实时同步的像素画布，所有用户在同一张画布上创作
- **注册/登录** — 用户名+邮箱+密码注册，支持用户名或邮箱登录
- **点数系统** — 每位用户拥有点数，放置像素消耗点数，随时间自动恢复
- **实时聊天** — 内置浮动聊天面板，在线用户可实时交流
- **管理后台** — 用户管理、聊天管理、画布重置/回滚、点数配置
- **多语言** — 支持 12 种语言（中/英/日/韩/法/德/西/葡/俄/阿/印/繁中）

## 技术栈

- **前端**: Next.js 16, React 19, Tailwind CSS 4
- **后端**: Next.js App Router API Routes, Socket.io
- **数据库**: PostgreSQL (Prisma ORM)
- **缓存**: Redis
- **认证**: JWT (jose) + bcryptjs
- **国际化**: next-intl

## 项目结构

```
pixel-canvas/
├── prisma/              # 数据库 Schema
├── server/              # Socket.io 服务端
├── src/
│   ├── app/
│   │   ├── [locale]/    # 多语言路由
│   │   └── api/         # API 路由
│   │       ├── auth/    # 认证 API (注册/登录/登出/用户信息)
│   │       ├── admin/   # 管理 API (配置/聊天/用户/重置/回滚)
│   │       ├── pixel/   # 像素 API
│   │       ├── canvas/  # 画布分块 API
│   │       ├── points/  # 点数 API
│   │       └── stats/   # 统计 API
│   ├── components/
│   │   ├── auth/        # 注册/登录组件
│   │   ├── canvas/      # 画布组件
│   │   ├── chat/        # 聊天组件
│   │   ├── points/      # 点数显示
│   │   ├── admin/       # 管理后台
│   │   └── ui/          # 通用 UI 组件
│   ├── hooks/           # 自定义 Hooks
│   └── lib/             # 工具函数
├── messages/            # 12 语言翻译文件
└── deploy/              # 部署脚本
```

## 本地开发

### 环境要求

- Node.js 20+
- PostgreSQL
- Redis

### 安装

```bash
# 克隆仓库
git clone https://github.com/JellyZYD/pixel-canvas.git
cd pixel-canvas

# 安装依赖
npm install

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入数据库和 Redis 连接信息

# 初始化数据库
npx prisma db push
npx prisma generate
```

### 运行

```bash
# 启动 Next.js 开发服务器
npm run dev

# 启动 Socket.io 服务器（另开一个终端）
npm run dev:socket
```

访问 http://localhost:3000

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | PostgreSQL 连接字符串 | - |
| `REDIS_URL` | Redis 连接字符串 | `redis://localhost:6379` |
| `ADMIN_PASSWORD` | 管理后台密码 | `admin123` |
| `NEXT_PUBLIC_ADMIN_PASSWORD` | 管理后台密码（客户端） | `admin123` |
| `JWT_SECRET` | JWT 签名密钥 | - |
| `SOCKET_PORT` | Socket.io 端口 | `3001` |
| `NEXT_PUBLIC_SOCKET_URL` | Socket.io 地址 | `http://localhost:3001` |
| `NEXT_PUBLIC_APP_URL` | 应用地址 | `http://localhost:3000` |

## 部署

### Ubuntu 服务器（推荐）

```bash
# 上传代码到服务器后执行
sudo bash deploy/setup.sh
```

脚本自动安装 Node.js、PostgreSQL、Redis、PM2、Nginx 并完成部署。

### 部署后操作

```bash
# 修改配置
nano /opt/pixel-canvas/.env

# 重启服务
pm2 restart all

# 查看日志
pm2 logs

# 启用 HTTPS
sudo certbot --nginx -d your-domain.com
```

### 更新代码

```bash
cd /opt/pixel-canvas
git pull origin main
bash deploy/update.sh
```

## API 文档

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册 |
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 获取当前用户 |

### 像素

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/canvas?cx=&cy=` | 获取画布分块 |
| GET | `/api/pixel/:x/:y` | 获取像素详情 |
| POST | `/api/pixel` | 放置像素 |

### 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/admin/config` | 获取/修改配置 |
| GET/DELETE | `/api/admin/chat` | 聊天管理 |
| GET/DELETE | `/api/admin/users` | 用户管理 |
| POST | `/api/admin/reset` | 重置画布 |
| POST | `/api/admin/rollback` | 回滚操作 |

## License

MIT
