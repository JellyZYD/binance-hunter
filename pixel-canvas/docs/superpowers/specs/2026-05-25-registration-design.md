# 注册功能设计规格

## 目标

为像素画布添加用户名+邮箱+密码注册/登录系统，替代当前的纯匿名用户模式。

## 架构

- 认证方式：JWT cookie（httpOnly，7天过期）
- 密码加密：bcrypt
- 注册模式：弹窗（模态框），顶栏按钮触发
- 数据迁移：注册时继承匿名用户的点数和像素历史

## 数据库变更

User 表新增字段：
- `email` String? @unique @db.VarChar(255) — 邮箱，可选（匿名用户无邮箱）
- `password` String? @db.VarChar(60) — bcrypt 哈希，可选（匿名用户无密码）

## API 端点

### POST /api/auth/register
- 输入：`{ username, email, password }`
- 验证：用户名 2-20 字符，邮箱格式，密码 6+ 字符
- 行为：创建用户或升级匿名用户，设置 JWT cookie
- 返回：`{ user: { id, nickname } }`

### POST /api/auth/login
- 输入：`{ login, password }`（login 可以是用户名或邮箱）
- 行为：验证密码，设置 JWT cookie
- 返回：`{ user: { id, nickname } }`

### POST /api/auth/logout
- 行为：清除 JWT cookie
- 返回：`{ success: true }`

### GET /api/auth/me
- 行为：从 cookie 读取 JWT，返回用户信息
- 返回：`{ user: { id, nickname } }` 或 `{ user: null }`

## 前端组件

### AuthModal
- 弹窗组件，包含"登录"和"注册"两个 tab
- 注册表单：用户名、邮箱、密码、确认密码
- 登录表单：用户名/邮箱、密码
- 错误提示（用户名已存在、密码错误等）

### UserMenu
- 顶栏右侧显示
- 未登录：显示"注册/登录"按钮
- 已登录：显示头像（用户名首字母）+ 昵称，点击展开下拉菜单（退出登录）

### 改造 usePoints 和 useSocket
- 登录后使用数据库 userId 替代 localStorage UUID
- 注册时将匿名用户的 Redis 点数数据迁移到新用户

## 数据迁移流程

1. 用户点击注册
2. 前端发送注册请求，附带当前 localStorage 的 userId
3. 后端创建新用户（带 email/password）
4. 后端将 Redis 中 `user:points:{oldUserId}` 的数据复制到 `user:points:{newUserId}`
5. 后端更新 PixelHistory 记录的 userId
6. 前端更新 localStorage 的 userId 为新用户 ID
7. 前端设置 JWT cookie

## 国际化

在 messages/*.json 中新增 `auth` 命名空间：
- `auth.register` — 注册
- `auth.login` — 登录
- `auth.logout` — 退出登录
- `auth.username` — 用户名
- `auth.email` — 邮箱
- `auth.password` — 密码
- `auth.confirmPassword` — 确认密码
- `auth.usernameExists` — 用户名已存在
- `auth.emailExists` — 邮箱已注册
- `auth.invalidCredentials` — 用户名或密码错误
- `auth.passwordMismatch` — 两次密码不一致
