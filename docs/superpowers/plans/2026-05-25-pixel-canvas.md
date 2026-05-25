# 协作像素画布 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 构建一个 2000×2000 协作像素画布，支持挂机攒点数、实时聊天、多语言、后台管理，投放 Google AdSense 广告获取收益。

**架构：** Next.js 14 App Router 前端 + Socket.io WebSocket 后端，Redis 缓存像素数据和点数，PostgreSQL 持久化用户和历史记录。前端部署 Vercel，后端部署新加坡 2核2G 服务器。

**技术栈：** Next.js 14, Tailwind CSS, next-intl, Canvas API, Socket.io, PostgreSQL, Redis, Prisma

---

## 文件结构

```
pixel-canvas/
├── prisma/
│   └── schema.prisma                    # 数据库模型定义
├── src/
│   ├── app/
│   │   ├── [locale]/
│   │   │   ├── layout.tsx               # 根布局（语言、字体）
│   │   │   ├── page.tsx                 # 主页面
│   │   │   └── admin/
│   │   │       └── page.tsx             # 后台管理页
│   │   ├── api/
│   │   │   ├── canvas/route.ts          # 画布数据 API
│   │   │   ├── pixel/route.ts           # 像素放置 API
│   │   │   ├── pixel/[x]/[y]/route.ts   # 像素查询 API
│   │   │   ├── stats/route.ts           # 统计 API
│   │   │   ├── auth/github/route.ts     # GitHub OAuth
│   │   │   ├── user/me/route.ts         # 用户信息 API
│   │   │   ├── admin/reset/route.ts     # 重置画布 API
│   │   │   └── admin/rollback/route.ts  # 回滚 API
│   │   └── layout.tsx                   # 全局布局
│   ├── components/
│   │   ├── canvas/
│   │   │   ├── PixelCanvas.tsx          # 画布主组件
│   │   │   ├── ColorPalette.tsx         # 颜色选择器
│   │   │   └── PixelInfo.tsx            # 像素信息浮窗
│   │   ├── chat/
│   │   │   ├── ChatPanel.tsx            # 聊天面板
│   │   │   └── ChatMessage.tsx          # 单条消息
│   │   ├── points/
│   │   │   └── PointsDisplay.tsx        # 点数显示 + 倒计时
│   │   ├── ui/
│   │   │   ├── LanguageSwitcher.tsx     # 语言切换器
│   │   │   └── AdBanner.tsx             # 广告位组件
│   │   └── admin/
│   │       ├── AdminPanel.tsx           # 管理面板
│   │       └── StatsCard.tsx            # 统计卡片
│   ├── lib/
│   │   ├── db.ts                        # Prisma 客户端
│   │   ├── redis.ts                     # Redis 客户端
│   │   ├── socket.ts                    # Socket.io 客户端
│   │   └── auth.ts                      # 认证工具
│   ├── hooks/
│   │   ├── useCanvas.ts                 # 画布交互 hook
│   │   ├── usePoints.ts                 # 点数管理 hook
│   │   └── useChat.ts                   # 聊天 hook
│   ├── i18n/
│   │   ├── request.ts                   # next-intl 请求配置
│   │   └── routing.ts                   # 路由配置
│   └── types/
│       └── index.ts                     # 公共类型定义
├── messages/                            # 翻译文件
│   ├── zh-CN.json
│   ├── zh-TW.json
│   ├── en.json
│   ├── ja.json
│   ├── ko.json
│   ├── fr.json
│   ├── de.json
│   ├── es.json
│   ├── pt.json
│   ├── ru.json
│   ├── ar.json
│   └── hi.json
├── server/
│   └── socket.ts                        # Socket.io 服务器
├── public/
│   └── favicon.ico
├── .env.example
├── .env.local
├── next.config.js
├── tailwind.config.ts
├── tsconfig.json
├── package.json
└── middleware.ts                         # next-intl 中间件
```

---

## 任务 1：项目初始化

**文件：**
- 创建：`package.json`, `next.config.js`, `tailwind.config.ts`, `tsconfig.json`, `.env.example`, `.env.local`

- [ ] **步骤 1：创建 Next.js 项目**

```bash
cd E:\workshop
npx create-next-app@latest pixel-canvas --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"
cd pixel-canvas
```

- [ ] **步骤 2：安装依赖**

```bash
npm install next-intl @prisma/client prisma socket.io socket.io-client ioredis uuid
npm install -D @types/uuid
```

- [ ] **步骤 3：创建环境变量文件**

`.env.example`:
```env
# Database
DATABASE_URL="postgresql://user:password@localhost:5432/pixel_canvas"
REDIS_URL="redis://localhost:6379"

# Auth
GITHUB_CLIENT_ID=""
GITHUB_CLIENT_SECRET=""
GITHUB_CALLBACK_URL=""

# Admin
ADMIN_PASSWORD=""

# Socket.io
SOCKET_PORT=3001
NEXT_PUBLIC_SOCKET_URL="http://localhost:3001"
```

- [ ] **步骤 4：配置 next-intl 中间件**

`middleware.ts`:
```typescript
import createMiddleware from 'next-intl/middleware';
import { locales, defaultLocale } from './src/i18n/routing';

export default createMiddleware({
  locales,
  defaultLocale,
  localePrefix: 'always'
});

export const config = {
  matcher: ['/', '/(zh-CN|zh-TW|en|ja|ko|fr|de|es|pt|ru|ar|hi)/:path*']
};
```

- [ ] **步骤 5：配置 next-intl 路由**

`src/i18n/routing.ts`:
```typescript
import { defineRouting } from 'next-intl/routing';

export const locales = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'pt', 'ru', 'ar', 'hi'] as const;
export const defaultLocale = 'zh-CN' as const;
export type Locale = (typeof locales)[number];

export const routing = defineRouting({
  locales,
  defaultLocale
});
```

`src/i18n/request.ts`:
```typescript
import { getRequestConfig } from 'next-intl/server';
import { routing } from './routing';

export default getRequestConfig(async ({ requestLocale }) => {
  let locale = await requestLocale;
  if (!locale || !routing.locales.includes(locale as any)) {
    locale = routing.defaultLocale;
  }
  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default
  };
});
```

- [ ] **步骤 6：配置 next.config.js**

```javascript
const createNextIntlPlugin = require('next-intl/plugin');
const withNextIntl = createNextIntlPlugin('./src/i18n/request.ts');

/** @type {import('next').NextConfig} */
const nextConfig = {};

module.exports = withNextIntl(nextConfig);
```

- [ ] **步骤 7：Commit**

```bash
git add .
git commit -m "feat: initialize Next.js project with next-intl and dependencies"
```

---

## 任务 2：数据库设置

**文件：**
- 创建：`prisma/schema.prisma`, `src/lib/db.ts`, `src/lib/redis.ts`

- [ ] **步骤 1：定义 Prisma Schema**

`prisma/schema.prisma`:
```prisma
generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model User {
  id           String   @id @default(uuid())
  nickname     String   @db.VarChar(50)
  githubId     String?  @unique @map("github_id") @db.VarChar(50)
  totalPixels  Int      @default(0) @map("total_pixels")
  createdAt    DateTime @default(now()) @map("created_at")
  pixels       PixelHistory[]

  @@map("users")
}

model PixelHistory {
  id        Int      @id @default(autoincrement())
  x         Int
  y         Int
  color     String   @db.VarChar(7)
  userId    String   @map("user_id")
  user      User     @relation(fields: [userId], references: [id])
  createdAt DateTime @default(now()) @map("created_at")

  @@index([x, y])
  @@index([createdAt])
  @@map("pixel_history")
}

model CanvasConfig {
  key   String @id @db.VarChar(50)
  value String

  @@map("canvas_config")
}
```

- [ ] **步骤 2：创建 Prisma 客户端**

`src/lib/db.ts`:
```typescript
import { PrismaClient } from '@prisma/client';

const globalForPrisma = globalThis as unknown as { prisma: PrismaClient };

export const prisma = globalForPrisma.prisma || new PrismaClient();

if (process.env.NODE_ENV !== 'production') {
  globalForPrisma.prisma = prisma;
}
```

- [ ] **步骤 3：创建 Redis 客户端**

`src/lib/redis.ts`:
```typescript
import Redis from 'ioredis';

const globalForRedis = globalThis as unknown as { redis: Redis };

export const redis = globalForRedis.redis || new Redis(process.env.REDIS_URL!);

if (process.env.NODE_ENV !== 'production') {
  globalForRedis.redis = redis;
}
```

- [ ] **步骤 4：运行数据库迁移**

```bash
npx prisma migrate dev --name init
```

- [ ] **步骤 5：Commit**

```bash
git add .
git commit -m "feat: add database schema and Redis/Prisma clients"
```

---

## 任务 3：公共类型定义

**文件：**
- 创建：`src/types/index.ts`

- [ ] **步骤 1：定义公共类型**

`src/types/index.ts`:
```typescript
export interface Pixel {
  x: number;
  y: number;
  color: string;
  userId: string;
  nickname: string;
  timestamp: number;
}

export interface PixelPlacement {
  x: number;
  y: number;
  color: string;
}

export interface ChatMessage {
  id: string;
  userId: string;
  nickname: string;
  content: string;
  timestamp: number;
}

export interface UserStats {
  totalPixels: number;
  joinedAt: string;
}

export interface CanvasStats {
  onlineUsers: number;
  totalPixels: number;
}

export const CANVAS_SIZE = 2000;
export const MAX_POINTS = 12;
export const POINT_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes
export const HEARTBEAT_INTERVAL_MS = 30 * 1000; // 30 seconds
export const CHAT_COOLDOWN_MS = 5 * 1000; // 5 seconds
export const MAX_MESSAGE_LENGTH = 200;

export const COLOR_PALETTE = [
  '#FFFFFF', '#C0C0C0', '#808080', '#000000',
  '#FF0000', '#FF4500', '#FFA500', '#FFD700',
  '#FFFF00', '#ADFF2F', '#00FF00', '#008000',
  '#00FFFF', '#0000FF', '#4B0082', '#8B00FF',
  '#FF69B4', '#FF1493', '#C71585', '#8B4513',
  '#A0522D', '#D2691E', '#F4A460', '#FFDEAD',
  '#E6E6FA', '#DDA0DD', '#9370DB', '#7B68EE',
  '#4169E1', '#1E90FF', '#87CEEB', '#B0E0E6',
];
```

- [ ] **步骤 2：Commit**

```bash
git add .
git commit -m "feat: add shared type definitions and constants"
```

---

## 任务 4：多语言翻译文件

**文件：**
- 创建：`messages/zh-CN.json`, `messages/en.json`, 其余 10 个语言文件

- [ ] **步骤 1：创建中文简体翻译**

`messages/zh-CN.json`:
```json
{
  "common": {
    "title": "像素画布",
    "subtitle": "合力绘制，每人一个像素",
    "loading": "加载中...",
    "error": "出错了",
    "save": "保存",
    "cancel": "取消",
    "confirm": "确认",
    "close": "关闭"
  },
  "canvas": {
    "zoomIn": "放大",
    "zoomOut": "缩小",
    "resetView": "重置视图",
    "placePixel": "放置像素",
    "pixelPlaced": "像素已放置！",
    "noPoints": "点数不足，请等待",
    "selectColor": "选择颜色",
    "customColor": "自定义颜色"
  },
  "points": {
    "title": "我的点数",
    "current": "当前点数：{count}",
    "nextPoint": "下一个点数：{time}",
    "maxReached": "已达上限",
    "cooldown": "冷却中"
  },
  "chat": {
    "title": "聊天室",
    "onlineUsers": "在线：{count} 人",
    "placeholder": "输入消息...",
    "send": "发送",
    "cooldown": "请等待 {seconds} 秒后再发",
    "maxLength": "最多 {count} 个字符",
    "noMessages": "暂无消息"
  },
  "user": {
    "guest": "游客",
    "login": "登录",
    "logout": "退出",
    "loginWith": "使用 GitHub 登录",
    "stats": "我的统计",
    "totalPixels": "总像素数：{count}",
    "joinedAt": "加入时间：{date}"
  },
  "pixelInfo": {
    "author": "作者：{name}",
    "placedAt": "放置时间：{time}",
    "coordinates": "坐标：({x}, {y})"
  },
  "admin": {
    "title": "后台管理",
    "password": "管理密码",
    "login": "登录管理",
    "reset": "重置画布",
    "resetConfirm": "确认重置画布？此操作不可撤销。",
    "rollback": "回滚操作",
    "rollbackCount": "回滚数量",
    "onlineUsers": "在线用户",
    "totalPixels": "总像素数",
    "adCode": "广告代码",
    "saveAdCode": "保存广告代码"
  },
  "language": {
    "label": "语言",
    "zh-CN": "中文简体",
    "zh-TW": "中文繁體",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "ru": "Русский",
    "ar": "العربية",
    "hi": "हिन्दी"
  }
}
```

- [ ] **步骤 2：创建英文翻译**

`messages/en.json`:
```json
{
  "common": {
    "title": "Pixel Canvas",
    "subtitle": "Create together, one pixel at a time",
    "loading": "Loading...",
    "error": "Something went wrong",
    "save": "Save",
    "cancel": "Cancel",
    "confirm": "Confirm",
    "close": "Close"
  },
  "canvas": {
    "zoomIn": "Zoom In",
    "zoomOut": "Zoom Out",
    "resetView": "Reset View",
    "placePixel": "Place Pixel",
    "pixelPlaced": "Pixel placed!",
    "noPoints": "Not enough points, please wait",
    "selectColor": "Select Color",
    "customColor": "Custom Color"
  },
  "points": {
    "title": "My Points",
    "current": "Points: {count}",
    "nextPoint": "Next point: {time}",
    "maxReached": "Max reached",
    "cooldown": "Cooling down"
  },
  "chat": {
    "title": "Chat",
    "onlineUsers": "Online: {count}",
    "placeholder": "Type a message...",
    "send": "Send",
    "cooldown": "Wait {seconds}s before sending again",
    "maxLength": "Max {count} characters",
    "noMessages": "No messages yet"
  },
  "user": {
    "guest": "Guest",
    "login": "Login",
    "logout": "Logout",
    "loginWith": "Login with GitHub",
    "stats": "My Stats",
    "totalPixels": "Total pixels: {count}",
    "joinedAt": "Joined: {date}"
  },
  "pixelInfo": {
    "author": "Author: {name}",
    "placedAt": "Placed at: {time}",
    "coordinates": "Coords: ({x}, {y})"
  },
  "admin": {
    "title": "Admin Panel",
    "password": "Admin Password",
    "login": "Login",
    "reset": "Reset Canvas",
    "resetConfirm": "Reset canvas? This cannot be undone.",
    "rollback": "Rollback",
    "rollbackCount": "Rollback count",
    "onlineUsers": "Online Users",
    "totalPixels": "Total Pixels",
    "adCode": "Ad Code",
    "saveAdCode": "Save Ad Code"
  },
  "language": {
    "label": "Language",
    "zh-CN": "中文简体",
    "zh-TW": "中文繁體",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "ru": "Русский",
    "ar": "العربية",
    "hi": "हिन्दी"
  }
}
```

- [ ] **步骤 3：创建其余 10 个语言文件**

为 `zh-TW`, `ja`, `ko`, `fr`, `de`, `es`, `pt`, `ru`, `ar`, `hi` 创建翻译文件，结构与 `en.json` 相同，翻译对应文案。

- [ ] **步骤 4：Commit**

```bash
git add messages/
git commit -m "feat: add i18n translation files for 12 languages"
```

---

## 任务 5：根布局与语言切换组件

**文件：**
- 创建：`src/app/layout.tsx`, `src/app/[locale]/layout.tsx`, `src/components/ui/LanguageSwitcher.tsx`
- 修改：`src/i18n/routing.ts`

- [ ] **步骤 1：创建全局布局**

`src/app/layout.tsx`:
```typescript
import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Pixel Canvas - 协作像素画布',
  description: '合力绘制巨型像素画，每人每5分钟一个像素点',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
```

- [ ] **步骤 2：创建语言布局**

`src/app/[locale]/layout.tsx`:
```typescript
import { NextIntlClientProvider } from 'next-intl';
import { getMessages } from 'next-intl/server';
import { notFound } from 'next/navigation';
import { locales } from '@/i18n/routing';

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

export default async function LocaleLayout({
  children,
  params: { locale },
}: {
  children: React.ReactNode;
  params: { locale: string };
}) {
  if (!locales.includes(locale as any)) notFound();

  const messages = await getMessages();

  return (
    <html lang={locale} dir={locale === 'ar' ? 'rtl' : 'ltr'}>
      <body className="bg-gray-900 text-white min-h-screen">
        <NextIntlClientProvider messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
```

- [ ] **步骤 3：创建语言切换组件**

`src/components/ui/LanguageSwitcher.tsx`:
```typescript
'use client';

import { useLocale, useTranslations } from 'next-intl';
import { useRouter, usePathname } from 'next/navigation';
import { useState, useRef, useEffect } from 'react';
import { locales, type Locale } from '@/i18n/routing';

const localeFlags: Record<Locale, string> = {
  'zh-CN': '🇨🇳',
  'zh-TW': '🇹🇼',
  'en': '🇺🇸',
  'ja': '🇯🇵',
  'ko': '🇰🇷',
  'fr': '🇫🇷',
  'de': '🇩🇪',
  'es': '🇪🇸',
  'pt': '🇧🇷',
  'ru': '🇷🇺',
  'ar': '🇸🇦',
  'hi': '🇮🇳',
};

export default function LanguageSwitcher() {
  const locale = useLocale() as Locale;
  const t = useTranslations('language');
  const router = useRouter();
  const pathname = usePathname();
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  function switchLocale(newLocale: Locale) {
    const path = pathname.replace(`/${locale}`, `/${newLocale}`);
    router.push(path);
    setIsOpen(false);
    localStorage.setItem('locale', newLocale);
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-2 bg-gray-800 rounded-lg hover:bg-gray-700 transition-colors"
      >
        <span>{localeFlags[locale]}</span>
        <span className="text-sm">{t(locale)}</span>
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute right-0 top-full mt-1 bg-gray-800 rounded-lg shadow-xl border border-gray-700 overflow-hidden z-50 min-w-[160px]">
          {locales.map((l) => (
            <button
              key={l}
              onClick={() => switchLocale(l)}
              className={`w-full flex items-center gap-2 px-4 py-2 text-sm hover:bg-gray-700 transition-colors ${
                l === locale ? 'bg-gray-700 text-blue-400' : ''
              }`}
            >
              <span>{localeFlags[l]}</span>
              <span>{t(l)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **步骤 4：Commit**

```bash
git add .
git commit -m "feat: add root layout and language switcher component"
```

---

## 任务 6：用户系统（匿名 + GitHub OAuth）

**文件：**
- 创建：`src/lib/auth.ts`, `src/app/api/auth/github/route.ts`, `src/app/api/user/me/route.ts`

- [ ] **步骤 1：创建认证工具**

`src/lib/auth.ts`:
```typescript
import { v4 as uuidv4 } from 'uuid';
import { prisma } from './db';

const nicknames = [
  '画师', '像素侠', '涂鸦客', '色彩师', '点阵王',
  '小画家', '像素狂', '涂色者', '画布师', '色块侠',
];

export function generateNickname(): string {
  const prefix = nicknames[Math.floor(Math.random() * nicknames.length)];
  const suffix = Math.floor(Math.random() * 9000) + 1000;
  return `${prefix}_${suffix}`;
}

export async function getOrCreateUser(userId?: string) {
  if (userId) {
    const existing = await prisma.user.findUnique({ where: { id: userId } });
    if (existing) return existing;
  }

  const id = userId || uuidv4();
  const nickname = generateNickname();

  return prisma.user.create({
    data: { id, nickname },
  });
}

export async function getUserFromGithub(githubId: string) {
  return prisma.user.findUnique({ where: { githubId } });
}

export async function linkGithub(userId: string, githubId: string) {
  return prisma.user.update({
    where: { id: userId },
    data: { githubId },
  });
}
```

- [ ] **步骤 2：创建 GitHub OAuth 路由**

`src/app/api/auth/github/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';

export async function GET(req: NextRequest) {
  const clientId = process.env.GITHUB_CLIENT_ID;
  const callbackUrl = process.env.GITHUB_CALLBACK_URL;

  const githubAuthUrl = `https://github.com/login/oauth/authorize?client_id=${clientId}&redirect_uri=${callbackUrl}&scope=read:user`;

  return NextResponse.redirect(githubAuthUrl);
}
```

- [ ] **步骤 3：创建 GitHub OAuth 回调路由**

`src/app/api/auth/github/callback/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';
import { generateNickname } from '@/lib/auth';

export async function GET(req: NextRequest) {
  const code = req.nextUrl.searchParams.get('code');

  if (!code) {
    return NextResponse.redirect(new URL('/', req.url));
  }

  // Exchange code for access token
  const tokenRes = await fetch('https://github.com/login/oauth/access_token', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({
      client_id: process.env.GITHUB_CLIENT_ID,
      client_secret: process.env.GITHUB_CLIENT_SECRET,
      code,
    }),
  });

  const { access_token } = await tokenRes.json();

  // Get user info
  const userRes = await fetch('https://api.github.com/user', {
    headers: { Authorization: `Bearer ${access_token}` },
  });

  const githubUser = await userRes.json();

  // Find or create user
  let user = await prisma.user.findUnique({
    where: { githubId: String(githubUser.id) },
  });

  if (!user) {
    user = await prisma.user.create({
      data: {
        nickname: githubUser.login || generateNickname(),
        githubId: String(githubUser.id),
      },
    });
  }

  // Set cookie and redirect
  const response = NextResponse.redirect(new URL('/', req.url));
  response.cookies.set('userId', user.id, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge: 60 * 60 * 24 * 365,
  });

  return response;
}
```

- [ ] **步骤 4：创建用户信息 API**

`src/app/api/user/me/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';

export async function GET(req: NextRequest) {
  const userId = req.cookies.get('userId')?.value;

  if (!userId) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const user = await prisma.user.findUnique({
    where: { id: userId },
    select: {
      id: true,
      nickname: true,
      githubId: true,
      totalPixels: true,
      createdAt: true,
    },
  });

  if (!user) {
    return NextResponse.json({ error: 'User not found' }, { status: 404 });
  }

  return NextResponse.json(user);
}
```

- [ ] **步骤 5：Commit**

```bash
git add .
git commit -m "feat: add user system with anonymous and GitHub OAuth"
```

---

## 任务 7：画布后端 API

**文件：**
- 创建：`src/app/api/canvas/route.ts`, `src/app/api/pixel/route.ts`, `src/app/api/pixel/[x]/[y]/route.ts`, `src/app/api/stats/route.ts`

- [ ] **步骤 1：创建画布数据 API**

`src/app/api/canvas/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { CANVAS_SIZE } from '@/types';

export async function GET(req: NextRequest) {
  const searchParams = req.nextUrl.searchParams;
  const chunkX = parseInt(searchParams.get('cx') || '0');
  const chunkY = parseInt(searchParams.get('cy') || '0');
  const chunkSize = 100;

  const startX = chunkX * chunkSize;
  const startY = chunkY * chunkSize;
  const pixels: Record<string, string> = {};

  // Get chunk pixels from Redis hash
  const pipeline = redis.pipeline();
  for (let x = startX; x < Math.min(startX + chunkSize, CANVAS_SIZE); x++) {
    for (let y = startY; y < Math.min(startY + chunkSize, CANVAS_SIZE); y++) {
      pipeline.hget('canvas:pixels', `${x},${y}`);
    }
  }

  const results = await pipeline.exec();

  let i = 0;
  for (let x = startX; x < Math.min(startX + chunkSize, CANVAS_SIZE); x++) {
    for (let y = startY; y < Math.min(startY + chunkSize, CANVAS_SIZE); y++) {
      const color = results?.[i]?.[1] as string | null;
      if (color) {
        pixels[`${x},${y}`] = color;
      }
      i++;
    }
  }

  return NextResponse.json({ chunkX, chunkY, chunkSize, pixels });
}
```

- [ ] **步骤 2：创建像素放置 API**

`src/app/api/pixel/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';
import { CANVAS_SIZE, MAX_POINTS } from '@/types';

export async function POST(req: NextRequest) {
  const userId = req.cookies.get('userId')?.value;
  if (!userId) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const { x, y, color } = await req.json();

  // Validate coordinates
  if (x < 0 || x >= CANVAS_SIZE || y < 0 || y >= CANVAS_SIZE) {
    return NextResponse.json({ error: 'Invalid coordinates' }, { status: 400 });
  }

  // Validate color format
  if (!/^#[0-9A-Fa-f]{6}$/.test(color)) {
    return NextResponse.json({ error: 'Invalid color' }, { status: 400 });
  }

  // Check points
  const points = parseInt((await redis.get(`user:points:${userId}`)) || '0');
  if (points < 1) {
    return NextResponse.json({ error: 'Not enough points' }, { status: 400 });
  }

  // Deduct point
  await redis.decr(`user:points:${userId}`);

  // Save pixel
  await redis.hset('canvas:pixels', `${x},${y}`, color);

  // Save to history
  await redis.lpush('canvas:history', JSON.stringify({ x, y, color, userId, timestamp: Date.now() }));
  await redis.ltrim('canvas:history', 0, 999);

  // Save to PostgreSQL
  const user = await prisma.user.findUnique({ where: { id: userId } });
  if (user) {
    await prisma.pixelHistory.create({
      data: { x, y, color, userId },
    });
    await prisma.user.update({
      where: { id: userId },
      data: { totalPixels: { increment: 1 } },
    });
  }

  return NextResponse.json({ success: true, points: points - 1 });
}
```

- [ ] **步骤 3：创建像素查询 API**

`src/app/api/pixel/[x]/[y]/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';

export async function GET(
  req: NextRequest,
  { params }: { params: { x: string; y: string } }
) {
  const x = parseInt(params.x);
  const y = parseInt(params.y);

  const record = await prisma.pixelHistory.findFirst({
    where: { x, y },
    orderBy: { createdAt: 'desc' },
    include: { user: { select: { nickname: true } } },
  });

  if (!record) {
    return NextResponse.json({ error: 'Pixel not found' }, { status: 404 });
  }

  return NextResponse.json({
    x: record.x,
    y: record.y,
    color: record.color,
    nickname: record.user.nickname,
    timestamp: record.createdAt.getTime(),
  });
}
```

- [ ] **步骤 4：创建统计 API**

`src/app/api/stats/route.ts`:
```typescript
import { NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';

export async function GET() {
  const onlineUsers = await redis.scard('online:users');
  const totalPixels = await prisma.pixelHistory.count();

  return NextResponse.json({ onlineUsers, totalPixels });
}
```

- [ ] **步骤 5：Commit**

```bash
git add .
git commit -m "feat: add canvas and pixel API routes"
```

---

## 任务 8：Socket.io 服务器

**文件：**
- 创建：`server/socket.ts`, `package.json` (scripts)

- [ ] **步骤 1：创建 Socket.io 服务器**

`server/socket.ts`:
```typescript
import { Server } from 'socket.io';
import Redis from 'ioredis';

const redis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
const PORT = parseInt(process.env.SOCKET_PORT || '3001');

const io = new Server(PORT, {
  cors: {
    origin: process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000',
    methods: ['GET', 'POST'],
  },
});

// Track connected users
const userSockets = new Map<string, string>(); // userId -> socketId

io.on('connection', (socket) => {
  const userId = socket.handshake.auth.userId as string;
  if (!userId) {
    socket.disconnect();
    return;
  }

  // Register user
  userSockets.set(userId, socket.id);
  redis.sadd('online:users', userId);

  // Broadcast online count
  redis.scard('online:users').then((count) => {
    io.emit('user:count', count);
  });

  // Send current points
  redis.get(`user:points:${userId}`).then((points) => {
    socket.emit('points:update', parseInt(points || '0'));
  });

  // Start heartbeat check
  redis.set(`user:heartbeat:${userId}`, Date.now().toString(), 'EX', 60);

  // Handle heartbeat
  socket.on('heartbeat', async () => {
    const lastHeartbeat = await redis.get(`user:heartbeat:${userId}`);
    const now = Date.now();

    if (lastHeartbeat) {
      const elapsed = now - parseInt(lastHeartbeat);
      // If more than 35 seconds since last heartbeat, grant a point tick
      if (elapsed >= 30000) {
        const currentPoints = parseInt((await redis.get(`user:points:${userId}`)) || '0');
        if (currentPoints < 12) {
          // Check if 5 minutes have passed since last point grant
          const lastGrant = await redis.get(`user:last_grant:${userId}`);
          if (!lastGrant || now - parseInt(lastGrant) >= 300000) {
            await redis.incr(`user:points:${userId}`);
            await redis.set(`user:last_grant:${userId}`, now.toString());
            const newPoints = parseInt((await redis.get(`user:points:${userId}`)) || '0');
            socket.emit('points:update', newPoints);
          }
        }
      }
    }

    await redis.set(`user:heartbeat:${userId}`, now.toString(), 'EX', 60);
  });

  // Handle pixel placement broadcast
  socket.on('pixel:update', (data) => {
    socket.broadcast.emit('pixel:update', data);
  });

  // Handle chat messages
  socket.on('chat:message', async (content: string) => {
    if (typeof content !== 'string' || content.length > 200 || content.trim().length === 0) {
      return;
    }

    // Rate limit: 5 seconds between messages
    const lastMessage = await redis.get(`user:chat_cooldown:${userId}`);
    const now = Date.now();
    if (lastMessage && now - parseInt(lastMessage) < 5000) {
      socket.emit('chat:cooldown', Math.ceil((5000 - (now - parseInt(lastMessage))) / 1000));
      return;
    }

    const nickname = socket.handshake.auth.nickname as string || 'Anonymous';
    const message = {
      id: `${userId}-${now}`,
      userId,
      nickname,
      content: content.trim(),
      timestamp: now,
    };

    // Save to Redis
    await redis.lpush('chat:messages', JSON.stringify(message));
    await redis.ltrim('chat:messages', 0, 49);
    await redis.set(`user:chat_cooldown:${userId}`, now.toString());

    // Broadcast
    io.emit('chat:message', message);
  });

  // Load chat history
  socket.on('chat:history', async () => {
    const messages = await redis.lrange('chat:messages', 0, 49);
    const parsed = messages.map((m) => JSON.parse(m)).reverse();
    socket.emit('chat:history', parsed);
  });

  // Handle disconnect
  socket.on('disconnect', async () => {
    userSockets.delete(userId);
    await redis.srem('online:users', userId);

    const count = await redis.scard('online:users');
    io.emit('user:count', count);
  });
});

console.log(`Socket.io server running on port ${PORT}`);
```

- [ ] **步骤 2：添加启动脚本**

`package.json` 添加 scripts:
```json
{
  "scripts": {
    "dev": "next dev",
    "dev:socket": "npx ts-node --project tsconfig.server.json server/socket.ts",
    "build": "next build",
    "start": "next start"
  }
}
```

创建 `tsconfig.server.json`:
```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "commonjs",
    "lib": ["ES2020"],
    "outDir": "./dist-server",
    "rootDir": "./server",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true
  },
  "include": ["server/**/*"]
}
```

- [ ] **步骤 3：Commit**

```bash
git add .
git commit -m "feat: add Socket.io server for real-time updates"
```

---

## 任务 9：画布前端组件

**文件：**
- 创建：`src/components/canvas/PixelCanvas.tsx`, `src/components/canvas/ColorPalette.tsx`, `src/components/canvas/PixelInfo.tsx`, `src/hooks/useCanvas.ts`

- [ ] **步骤 1：创建画布交互 Hook**

`src/hooks/useCanvas.ts`:
```typescript
'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import { CANVAS_SIZE, COLOR_PALETTE } from '@/types';

interface CanvasState {
  scale: number;
  offsetX: number;
  offsetY: number;
  selectedColor: string;
  isDragging: boolean;
}

export function useCanvas() {
  const [state, setState] = useState<CanvasState>({
    scale: 0.5,
    offsetX: 0,
    offsetY: 0,
    selectedColor: COLOR_PALETTE[0],
    isDragging: false,
  });

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const lastPos = useRef({ x: 0, y: 0 });

  const zoom = useCallback((delta: number, centerX: number, centerY: number) => {
    setState((prev) => {
      const factor = delta > 0 ? 0.9 : 1.1;
      const newScale = Math.max(0.1, Math.min(20, prev.scale * factor));
      const scaleChange = newScale / prev.scale;

      return {
        ...prev,
        scale: newScale,
        offsetX: centerX - (centerX - prev.offsetX) * scaleChange,
        offsetY: centerY - (centerY - prev.offsetY) * scaleChange,
      };
    });
  }, []);

  const pan = useCallback((dx: number, dy: number) => {
    setState((prev) => ({
      ...prev,
      offsetX: prev.offsetX + dx,
      offsetY: prev.offsetY + dy,
    }));
  }, []);

  const getPixelCoord = useCallback((clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;

    const rect = canvas.getBoundingClientRect();
    const x = Math.floor((clientX - rect.left - state.offsetX) / state.scale);
    const y = Math.floor((clientY - rect.top - state.offsetY) / state.scale);

    if (x >= 0 && x < CANVAS_SIZE && y >= 0 && y < CANVAS_SIZE) {
      return { x, y };
    }
    return null;
  }, [state.offsetX, state.offsetY, state.scale]);

  const setSelectedColor = useCallback((color: string) => {
    setState((prev) => ({ ...prev, selectedColor: color }));
  }, []);

  const resetView = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    setState((prev) => ({
      ...prev,
      scale: Math.min(canvas.width, canvas.height) / CANVAS_SIZE,
      offsetX: 0,
      offsetY: 0,
    }));
  }, []);

  return {
    ...state,
    canvasRef,
    zoom,
    pan,
    getPixelCoord,
    setSelectedColor,
    resetView,
  };
}
```

- [ ] **步骤 2：创建颜色选择器组件**

`src/components/canvas/ColorPalette.tsx`:
```typescript
'use client';

import { useTranslations } from 'next-intl';
import { COLOR_PALETTE } from '@/types';

interface Props {
  selectedColor: string;
  onColorSelect: (color: string) => void;
}

export default function ColorPalette({ selectedColor, onColorSelect }: Props) {
  const t = useTranslations('canvas');

  return (
    <div className="bg-gray-800 rounded-lg p-3">
      <p className="text-sm text-gray-400 mb-2">{t('selectColor')}</p>
      <div className="grid grid-cols-8 gap-1">
        {COLOR_PALETTE.map((color) => (
          <button
            key={color}
            onClick={() => onColorSelect(color)}
            className={`w-6 h-6 rounded border-2 transition-transform hover:scale-110 ${
              selectedColor === color ? 'border-white scale-110' : 'border-gray-600'
            }`}
            style={{ backgroundColor: color }}
            title={color}
          />
        ))}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="color"
          value={selectedColor}
          onChange={(e) => onColorSelect(e.target.value)}
          className="w-8 h-8 cursor-pointer"
        />
        <span className="text-xs text-gray-400">{t('customColor')}</span>
      </div>
    </div>
  );
}
```

- [ ] **步骤 3：创建像素信息浮窗**

`src/components/canvas/PixelInfo.tsx`:
```typescript
'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useState } from 'react';

interface Props {
  x: number;
  y: number;
  onClose: () => void;
}

interface PixelData {
  nickname: string;
  timestamp: number;
  color: string;
}

export default function PixelInfo({ x, y, onClose }: Props) {
  const t = useTranslations('pixelInfo');
  const [data, setData] = useState<PixelData | null>(null);

  useEffect(() => {
    fetch(`/api/pixel/${x}/${y}`)
      .then((res) => res.json())
      .then(setData)
      .catch(() => setData(null));
  }, [x, y]);

  return (
    <div className="absolute bg-gray-800 rounded-lg p-3 shadow-xl border border-gray-700 z-50 min-w-[200px]">
      <div className="flex justify-between items-start mb-2">
        <span className="text-xs text-gray-400">{t('coordinates', { x, y })}</span>
        <button onClick={onClose} className="text-gray-400 hover:text-white">
          &times;
        </button>
      </div>
      {data ? (
        <>
          <div className="flex items-center gap-2 mb-1">
            <div className="w-4 h-4 rounded" style={{ backgroundColor: data.color }} />
            <span className="text-sm font-medium">{t('author', { name: data.nickname })}</span>
          </div>
          <p className="text-xs text-gray-400">
            {t('placedAt', { time: new Date(data.timestamp).toLocaleString() })}
          </p>
        </>
      ) : (
        <p className="text-sm text-gray-400">-</p>
      )}
    </div>
  );
}
```

- [ ] **步骤 4：创建画布主组件**

`src/components/canvas/PixelCanvas.tsx`:
```typescript
'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { useCanvas } from '@/hooks/useCanvas';
import { useSocket } from '@/lib/socket';
import ColorPalette from './ColorPalette';
import PixelInfo from './PixelInfo';
import { CANVAS_SIZE } from '@/types';
import { useTranslations } from 'next-intl';

interface PixelUpdate {
  x: number;
  y: number;
  color: string;
}

export default function PixelCanvas() {
  const t = useTranslations('canvas');
  const {
    scale,
    offsetX,
    offsetY,
    selectedColor,
    canvasRef,
    zoom,
    pan,
    getPixelCoord,
    setSelectedColor,
    resetView,
  } = useCanvas();

  const socket = useSocket();
  const [pixelInfo, setPixelInfo] = useState<{ x: number; y: number } | null>(null);
  const [points, setPoints] = useState(0);
  const pixelsRef = useRef<Map<string, string>>(new Map());
  const pendingPixelsRef = useRef<PixelUpdate[]>([]);

  // Load initial canvas data
  useEffect(() => {
    async function loadChunks() {
      const chunks = 20; // 2000 / 100
      for (let cx = 0; cx < chunks; cx++) {
        for (let cy = 0; cy < chunks; cy++) {
          const res = await fetch(`/api/canvas?cx=${cx}&cy=${cy}`);
          const data = await res.json();
          Object.entries(data.pixels).forEach(([key, color]) => {
            pixelsRef.current.set(key, color as string);
          });
        }
      }
      renderCanvas();
    }
    loadChunks();
  }, []);

  // Listen for real-time updates
  useEffect(() => {
    if (!socket) return;

    socket.on('pixel:update', (data: PixelUpdate) => {
      pixelsRef.current.set(`${data.x},${data.y}`, data.color);
      pendingPixelsRef.current.push(data);
    });

    socket.on('points:update', (newPoints: number) => {
      setPoints(newPoints);
    });

    return () => {
      socket.off('pixel:update');
      socket.off('points:update');
    };
  }, [socket]);

  // Render loop
  const renderCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.translate(offsetX, offsetY);
    ctx.scale(scale, scale);

    // Draw pixels
    pixelsRef.current.forEach((color, key) => {
      const [x, y] = key.split(',').map(Number);
      ctx.fillStyle = color;
      ctx.fillRect(x, y, 1, 1);
    });

    ctx.restore();

    // Request next frame
    requestAnimationFrame(renderCanvas);
  }, [scale, offsetX, offsetY]);

  useEffect(() => {
    const frameId = requestAnimationFrame(renderCanvas);
    return () => cancelAnimationFrame(frameId);
  }, [renderCanvas]);

  // Handle mouse wheel zoom
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    zoom(e.deltaY, e.clientX, e.clientY);
  }, [zoom]);

  // Handle mouse drag
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button === 0) {
      const pos = getPixelCoord(e.clientX, e.clientY);
      if (pos) {
        setPixelInfo(pos);
      }
    }
  }, [getPixelCoord]);

  // Handle pixel placement
  const handlePlacePixel = useCallback(async () => {
    if (!pixelInfo || points < 1) return;

    const { x, y } = pixelInfo;
    const color = selectedColor;

    // Optimistic update
    pixelsRef.current.set(`${x},${y}`, color);

    // Send to server
    const res = await fetch('/api/pixel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y, color }),
    });

    if (res.ok) {
      const data = await res.json();
      setPoints(data.points);

      // Broadcast to others
      socket?.emit('pixel:update', { x, y, color });
    } else {
      // Revert on failure
      pixelsRef.current.delete(`${x},${y}`);
    }

    setPixelInfo(null);
  }, [pixelInfo, points, selectedColor, socket]);

  return (
    <div className="relative flex-1 overflow-hidden bg-gray-950">
      {/* Canvas */}
      <canvas
        ref={canvasRef}
        width={typeof window !== 'undefined' ? window.innerWidth : 1920}
        height={typeof window !== 'undefined' ? window.innerHeight : 1080}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        className="cursor-crosshair"
      />

      {/* Color Palette */}
      <div className="absolute bottom-4 left-4">
        <ColorPalette selectedColor={selectedColor} onColorSelect={setSelectedColor} />
      </div>

      {/* Points Display */}
      <div className="absolute top-4 left-4 bg-gray-800 rounded-lg p-3">
        <p className="text-sm text-gray-400">{t('placePixel')}</p>
        <p className="text-2xl font-bold">{points}</p>
      </div>

      {/* Place Button */}
      {pixelInfo && (
        <button
          onClick={handlePlacePixel}
          disabled={points < 1}
          className="absolute bottom-20 left-1/2 -translate-x-1/2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 px-6 py-3 rounded-full font-bold transition-colors"
        >
          {points < 1 ? t('noPoints') : t('placePixel')}
        </button>
      )}

      {/* Pixel Info */}
      {pixelInfo && (
        <PixelInfo
          x={pixelInfo.x}
          y={pixelInfo.y}
          onClose={() => setPixelInfo(null)}
        />
      )}

      {/* Zoom Controls */}
      <div className="absolute top-4 right-4 flex flex-col gap-2">
        <button
          onClick={() => zoom(-1, window.innerWidth / 2, window.innerHeight / 2)}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center"
        >
          +
        </button>
        <button
          onClick={() => zoom(1, window.innerWidth / 2, window.innerHeight / 2)}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center"
        >
          -
        </button>
        <button
          onClick={resetView}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center text-xs"
        >
          R
        </button>
      </div>
    </div>
  );
}
```

- [ ] **步骤 5：Commit**

```bash
git add .
git commit -m "feat: add canvas frontend components with zoom/pan/pixel placement"
```

---

## 任务 10：聊天面板组件

**文件：**
- 创建：`src/components/chat/ChatPanel.tsx`, `src/components/chat/ChatMessage.tsx`, `src/hooks/useChat.ts`

- [ ] **步骤 1：创建聊天 Hook**

`src/hooks/useChat.ts`:
```typescript
'use client';

import { useState, useEffect, useCallback } from 'react';
import { useSocket } from '@/lib/socket';
import { ChatMessage, CHAT_COOLDOWN_MS } from '@/types';

export function useChat() {
  const socket = useSocket();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cooldown, setCooldown] = useState(0);
  const [onlineCount, setOnlineCount] = useState(0);

  useEffect(() => {
    if (!socket) return;

    // Load history
    socket.emit('chat:history');
    socket.on('chat:history', (history: ChatMessage[]) => {
      setMessages(history);
    });

    // Listen for new messages
    socket.on('chat:message', (message: ChatMessage) => {
      setMessages((prev) => [...prev.slice(-49), message]);
    });

    // Listen for online count
    socket.on('user:count', (count: number) => {
      setOnlineCount(count);
    });

    // Listen for cooldown
    socket.on('chat:cooldown', (seconds: number) => {
      setCooldown(seconds);
    });

    return () => {
      socket.off('chat:history');
      socket.off('chat:message');
      socket.off('user:count');
      socket.off('chat:cooldown');
    };
  }, [socket]);

  // Cooldown timer
  useEffect(() => {
    if (cooldown <= 0) return;

    const timer = setInterval(() => {
      setCooldown((prev) => Math.max(0, prev - 1));
    }, 1000);

    return () => clearInterval(timer);
  }, [cooldown]);

  const sendMessage = useCallback((content: string) => {
    if (!socket || cooldown > 0) return;
    socket.emit('chat:message', content);
  }, [socket, cooldown]);

  return { messages, sendMessage, cooldown, onlineCount };
}
```

- [ ] **步骤 2：创建聊天消息组件**

`src/components/chat/ChatMessage.tsx`:
```typescript
'use client';

import { ChatMessage as MessageType } from '@/types';

interface Props {
  message: MessageType;
}

export default function ChatMessage({ message }: Props) {
  const time = new Date(message.timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });

  return (
    <div className="py-1 px-2 hover:bg-gray-800 rounded">
      <span className="text-xs text-gray-500 mr-2">{time}</span>
      <span className="text-sm font-medium text-blue-400">{message.nickname}</span>
      <span className="text-sm text-gray-300">: {message.content}</span>
    </div>
  );
}
```

- [ ] **步骤 3：创建聊天面板组件**

`src/components/chat/ChatPanel.tsx`:
```typescript
'use client';

import { useState, useRef, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { useChat } from '@/hooks/useChat';
import ChatMessage from './ChatMessage';
import { MAX_MESSAGE_LENGTH } from '@/types';

export default function ChatPanel() {
  const t = useTranslations('chat');
  const { messages, sendMessage, cooldown, onlineCount } = useChat();
  const [input, setInput] = useState('');
  const [isOpen, setIsOpen] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = () => {
    if (input.trim() && cooldown === 0) {
      sendMessage(input.trim());
      setInput('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className={`flex flex-col bg-gray-800 transition-all ${isOpen ? 'w-80' : 'w-12'}`}>
      {/* Toggle Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="h-10 flex items-center justify-center bg-gray-700 hover:bg-gray-600"
      >
        {isOpen ? '>' : '<'}
      </button>

      {isOpen && (
        <>
          {/* Header */}
          <div className="p-3 border-b border-gray-700">
            <h3 className="font-bold">{t('title')}</h3>
            <p className="text-xs text-gray-400">
              {t('onlineUsers', { count: onlineCount })}
            </p>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {messages.length === 0 ? (
              <p className="text-sm text-gray-500 text-center py-4">
                {t('noMessages')}
              </p>
            ) : (
              messages.map((msg) => <ChatMessage key={msg.id} message={msg} />)
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="p-3 border-t border-gray-700">
            <div className="flex gap-2">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value.slice(0, MAX_MESSAGE_LENGTH))}
                onKeyDown={handleKeyDown}
                placeholder={t('placeholder')}
                disabled={cooldown > 0}
                className="flex-1 bg-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
              />
              <button
                onClick={handleSend}
                disabled={cooldown > 0 || !input.trim()}
                className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 px-4 py-2 rounded text-sm font-medium transition-colors"
              >
                {cooldown > 0 ? `${cooldown}s` : t('send')}
              </button>
            </div>
            <p className="text-xs text-gray-500 mt-1">
              {input.length}/{MAX_MESSAGE_LENGTH}
            </p>
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **步骤 4：Commit**

```bash
git add .
git commit -m "feat: add chat panel with real-time messaging"
```

---

## 任务 11：点数显示组件

**文件：**
- 创建：`src/components/points/PointsDisplay.tsx`, `src/hooks/usePoints.ts`

- [ ] **步骤 1：创建点数 Hook**

`src/hooks/usePoints.ts`:
```typescript
'use client';

import { useState, useEffect, useRef } from 'react';
import { useSocket } from '@/lib/socket';
import { MAX_POINTS, POINT_INTERVAL_MS, HEARTBEAT_INTERVAL_MS } from '@/types';

export function usePoints() {
  const socket = useSocket();
  const [points, setPoints] = useState(0);
  const [nextPointIn, setNextPointIn] = useState(0);
  const lastGrantRef = useRef<number>(Date.now());
  const heartbeatRef = useRef<NodeJS.Timeout>();

  useEffect(() => {
    if (!socket) return;

    socket.on('points:update', (newPoints: number) => {
      setPoints(newPoints);
      lastGrantRef.current = Date.now();
    });

    // Start heartbeat
    heartbeatRef.current = setInterval(() => {
      socket.emit('heartbeat');
    }, HEARTBEAT_INTERVAL_MS);

    // Initial heartbeat
    socket.emit('heartbeat');

    return () => {
      socket.off('points:update');
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
      }
    };
  }, [socket]);

  // Countdown timer
  useEffect(() => {
    if (points >= MAX_POINTS) {
      setNextPointIn(0);
      return;
    }

    const timer = setInterval(() => {
      const elapsed = Date.now() - lastGrantRef.current;
      const remaining = Math.max(0, POINT_INTERVAL_MS - elapsed);
      setNextPointIn(remaining);
    }, 1000);

    return () => clearInterval(timer);
  }, [points]);

  // Pause on blur
  useEffect(() => {
    const handleBlur = () => {
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
      }
    };

    const handleFocus = () => {
      if (socket) {
        heartbeatRef.current = setInterval(() => {
          socket.emit('heartbeat');
        }, HEARTBEAT_INTERVAL_MS);
        socket.emit('heartbeat');
      }
    };

    window.addEventListener('blur', handleBlur);
    window.addEventListener('focus', handleFocus);

    return () => {
      window.removeEventListener('blur', handleBlur);
      window.removeEventListener('focus', handleFocus);
    };
  }, [socket]);

  return { points, nextPointIn, maxPoints: MAX_POINTS };
}
```

- [ ] **步骤 2：创建点数显示组件**

`src/components/points/PointsDisplay.tsx`:
```typescript
'use client';

import { useTranslations } from 'next-intl';
import { usePoints } from '@/hooks/usePoints';

export default function PointsDisplay() {
  const t = useTranslations('points');
  const { points, nextPointIn, maxPoints } = usePoints();

  const formatTime = (ms: number) => {
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return `${minutes}:${seconds.toString().padStart(2, '0')}`;
  };

  return (
    <div className="bg-gray-800 rounded-lg p-4">
      <h3 className="text-sm text-gray-400 mb-2">{t('title')}</h3>
      <div className="flex items-baseline gap-2">
        <span className="text-3xl font-bold text-blue-400">{points}</span>
        <span className="text-sm text-gray-500">/ {maxPoints}</span>
      </div>

      {points < maxPoints && (
        <div className="mt-2">
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>{t('nextPoint')}</span>
            <span>{formatTime(nextPointIn)}</span>
          </div>
          <div className="w-full bg-gray-700 rounded-full h-2">
            <div
              className="bg-blue-600 h-2 rounded-full transition-all"
              style={{ width: `${((POINT_INTERVAL - nextPointIn) / POINT_INTERVAL) * 100}%` }}
            />
          </div>
        </div>
      )}

      {points >= maxPoints && (
        <p className="text-xs text-green-400 mt-1">{t('maxReached')}</p>
      )}
    </div>
  );
}
```

注意：需要在文件顶部导入 `POINT_INTERVAL_MS`：
```typescript
import { POINT_INTERVAL_MS as POINT_INTERVAL } from '@/types';
```

- [ ] **步骤 3：Commit**

```bash
git add .
git commit -m "feat: add points display with countdown timer"
```

---

## 任务 12：广告位组件

**文件：**
- 创建：`src/components/ui/AdBanner.tsx`

- [ ] **步骤 1：创建广告位组件**

`src/components/ui/AdBanner.tsx`:
```typescript
'use client';

import { useEffect, useRef } from 'react';

interface Props {
  slot: string;
  format?: 'horizontal' | 'rectangle' | 'small';
  className?: string;
}

export default function AdBanner({ slot, format = 'horizontal', className = '' }: Props) {
  const adRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Load Google AdSense
    try {
      ((window as any).adsbygoogle = (window as any).adsbygoogle || []).push({});
    } catch (err) {
      console.error('AdSense error:', err);
    }
  }, []);

  const dimensions = {
    horizontal: { width: 728, height: 90 },
    rectangle: { width: 300, height: 250 },
    small: { width: 320, height: 50 },
  };

  const { width, height } = dimensions[format];

  return (
    <div ref={adRef} className={`ad-container ${className}`}>
      <ins
        className="adsbygoogle"
        style={{ display: 'inline-block', width, height }}
        data-ad-client={process.env.NEXT_PUBLIC_ADSENSE_CLIENT}
        data-ad-slot={slot}
        data-ad-format="auto"
        data-full-width-responsive="true"
      />
    </div>
  );
}
```

- [ ] **步骤 2：Commit**

```bash
git add .
git commit -m "feat: add Google AdSense banner component"
```

---

## 任务 13：主页面整合

**文件：**
- 创建：`src/app/[locale]/page.tsx`

- [ ] **步骤 1：创建主页面**

`src/app/[locale]/page.tsx`:
```typescript
'use client';

import { useTranslations } from 'next-intl';
import dynamic from 'next/dynamic';
import LanguageSwitcher from '@/components/ui/LanguageSwitcher';
import AdBanner from '@/components/ui/AdBanner';

const PixelCanvas = dynamic(() => import('@/components/canvas/PixelCanvas'), { ssr: false });
const ChatPanel = dynamic(() => import('@/components/chat/ChatPanel'), { ssr: false });
const PointsDisplay = dynamic(() => import('@/components/points/PointsDisplay'), { ssr: false });

export default function HomePage() {
  const t = useTranslations();

  return (
    <div className="h-screen flex flex-col">
      {/* Header */}
      <header className="bg-gray-800 border-b border-gray-700 px-4 py-2 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">{t('common.title')}</h1>
          <p className="text-xs text-gray-400">{t('common.subtitle')}</p>
        </div>
        <div className="flex items-center gap-4">
          <PointsDisplay />
          <LanguageSwitcher />
        </div>
      </header>

      {/* Top Ad */}
      <AdBanner slot="top-banner" format="horizontal" className="flex justify-center py-2 bg-gray-900" />

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Canvas */}
        <PixelCanvas />

        {/* Sidebar */}
        <div className="flex flex-col">
          <ChatPanel />
          {/* Side Ad */}
          <AdBanner slot="side-rectangle" format="rectangle" className="p-2 bg-gray-800" />
        </div>
      </div>

      {/* Footer */}
      <footer className="bg-gray-800 border-t border-gray-700 px-4 py-2 text-center text-xs text-gray-500">
        Pixel Canvas &copy; {new Date().getFullYear()}
      </footer>
    </div>
  );
}
```

- [ ] **步骤 2：Commit**

```bash
git add .
git commit -m "feat: add main page integrating canvas, chat, and ads"
```

---

## 任务 14：后台管理页面

**文件：**
- 创建：`src/app/[locale]/admin/page.tsx`, `src/components/admin/AdminPanel.tsx`, `src/app/api/admin/reset/route.ts`, `src/app/api/admin/rollback/route.ts`

- [ ] **步骤 1：创建管理面板组件**

`src/components/admin/AdminPanel.tsx`:
```typescript
'use client';

import { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';

interface Stats {
  onlineUsers: number;
  totalPixels: number;
}

export default function AdminPanel() {
  const t = useTranslations('admin');
  const [password, setPassword] = useState('');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [stats, setStats] = useState<Stats>({ onlineUsers: 0, totalPixels: 0 });
  const [rollbackCount, setRollbackCount] = useState(100);
  const [adCode, setAdCode] = useState('');

  useEffect(() => {
    if (!isAuthenticated) return;

    const fetchStats = async () => {
      const res = await fetch('/api/stats');
      const data = await res.json();
      setStats(data);
    };

    fetchStats();
    const interval = setInterval(fetchStats, 5000);
    return () => clearInterval(interval);
  }, [isAuthenticated]);

  const handleLogin = () => {
    // Simple password check - in production, use proper auth
    if (password === process.env.NEXT_PUBLIC_ADMIN_PASSWORD) {
      setIsAuthenticated(true);
    }
  };

  const handleReset = async () => {
    if (!confirm(t('resetConfirm'))) return;

    await fetch('/api/admin/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });

    alert('Canvas reset!');
  };

  const handleRollback = async () => {
    await fetch('/api/admin/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: { password, count: rollbackCount },
    });

    alert(`Rolled back ${rollbackCount} pixels!`);
  };

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-900">
        <div className="bg-gray-800 p-8 rounded-lg w-96">
          <h2 className="text-2xl font-bold mb-6">{t('title')}</h2>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t('password')}
            className="w-full bg-gray-700 rounded px-4 py-3 mb-4 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <button
            onClick={handleLogin}
            className="w-full bg-blue-600 hover:bg-blue-700 py-3 rounded font-bold transition-colors"
          >
            {t('login')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-900 p-8">
      <h1 className="text-3xl font-bold mb-8">{t('title')}</h1>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-4 mb-8">
        <div className="bg-gray-800 p-6 rounded-lg">
          <p className="text-sm text-gray-400">{t('onlineUsers')}</p>
          <p className="text-4xl font-bold text-blue-400">{stats.onlineUsers}</p>
        </div>
        <div className="bg-gray-800 p-6 rounded-lg">
          <p className="text-sm text-gray-400">{t('totalPixels')}</p>
          <p className="text-4xl font-bold text-green-400">{stats.totalPixels}</p>
        </div>
      </div>

      {/* Actions */}
      <div className="grid grid-cols-2 gap-4 mb-8">
        <div className="bg-gray-800 p-6 rounded-lg">
          <h3 className="font-bold mb-4">{t('reset')}</h3>
          <button
            onClick={handleReset}
            className="w-full bg-red-600 hover:bg-red-700 py-2 rounded transition-colors"
          >
            {t('reset')}
          </button>
        </div>

        <div className="bg-gray-800 p-6 rounded-lg">
          <h3 className="font-bold mb-4">{t('rollback')}</h3>
          <input
            type="number"
            value={rollbackCount}
            onChange={(e) => setRollbackCount(parseInt(e.target.value))}
            className="w-full bg-gray-700 rounded px-3 py-2 mb-3 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <button
            onClick={handleRollback}
            className="w-full bg-yellow-600 hover:bg-yellow-700 py-2 rounded transition-colors"
          >
            {t('rollback')}
          </button>
        </div>
      </div>

      {/* Ad Code */}
      <div className="bg-gray-800 p-6 rounded-lg">
        <h3 className="font-bold mb-4">{t('adCode')}</h3>
        <textarea
          value={adCode}
          onChange={(e) => setAdCode(e.target.value)}
          className="w-full bg-gray-700 rounded px-3 py-2 h-32 font-mono text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          placeholder="<script>...</script>"
        />
        <button
          onClick={() => {/* Save ad code */}}
          className="mt-3 bg-green-600 hover:bg-green-700 px-6 py-2 rounded transition-colors"
        >
          {t('saveAdCode')}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **步骤 2：创建管理页面**

`src/app/[locale]/admin/page.tsx`:
```typescript
'use client';

import AdminPanel from '@/components/admin/AdminPanel';

export default function AdminPage() {
  return <AdminPanel />;
}
```

- [ ] **步骤 3：创建重置 API**

`src/app/api/admin/reset/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';

export async function POST(req: NextRequest) {
  const { password } = await req.json();

  if (password !== process.env.ADMIN_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  // Clear Redis canvas data
  await redis.del('canvas:pixels');
  await redis.del('canvas:history');

  // Clear PostgreSQL history
  await prisma.pixelHistory.deleteMany();
  await prisma.user.updateMany({
    data: { totalPixels: 0 },
  });

  return NextResponse.json({ success: true });
}
```

- [ ] **步骤 4：创建回滚 API**

`src/app/api/admin/rollback/route.ts`:
```typescript
import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';

export async function POST(req: NextRequest) {
  const { password, count } = await req.json();

  if (password !== process.env.ADMIN_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  // Get recent history
  const history = await prisma.pixelHistory.findMany({
    orderBy: { createdAt: 'desc' },
    take: count,
  });

  // Delete from PostgreSQL
  await prisma.pixelHistory.deleteMany({
    where: {
      id: { in: history.map((p) => p.id) },
    },
  });

  // Update Redis - restore previous colors or delete
  const pipeline = redis.pipeline();
  for (const pixel of history) {
    // Find previous pixel at this location
    const prev = await prisma.pixelHistory.findFirst({
      where: {
        x: pixel.x,
        y: pixel.y,
        createdAt: { lt: pixel.createdAt },
      },
      orderBy: { createdAt: 'desc' },
    });

    if (prev) {
      pipeline.hset('canvas:pixels', `${pixel.x},${pixel.y}`, prev.color);
    } else {
      pipeline.hdel('canvas:pixels', `${pixel.x},${pixel.y}`);
    }
  }
  await pipeline.exec();

  return NextResponse.json({ success: true, rolledBack: history.length });
}
```

- [ ] **步骤 5：Commit**

```bash
git add .
git commit -m "feat: add admin panel with reset and rollback"
```

---

## 任务 15：Socket.io 客户端工具

**文件：**
- 创建：`src/lib/socket.ts`

- [ ] **步骤 1：创建 Socket 客户端**

`src/lib/socket.ts`:
```typescript
'use client';

import { io, Socket } from 'socket.io-client';
import { useEffect, useState, useRef } from 'react';

let socket: Socket | null = null;

export function getSocket(userId: string, nickname: string): Socket {
  if (!socket) {
    socket = io(process.env.NEXT_PUBLIC_SOCKET_URL || 'http://localhost:3001', {
      auth: { userId, nickname },
      autoConnect: true,
    });
  }
  return socket;
}

export function useSocket() {
  const [connectedSocket, setConnectedSocket] = useState<Socket | null>(null);

  useEffect(() => {
    // Get user ID from localStorage or cookie
    let userId = localStorage.getItem('userId');
    if (!userId) {
      userId = crypto.randomUUID();
      localStorage.setItem('userId', userId);
    }

    const nickname = localStorage.getItem('nickname') || `Guest_${Math.floor(Math.random() * 9000) + 1000}`;

    const s = getSocket(userId, nickname);
    setConnectedSocket(s);

    return () => {
      // Don't disconnect on unmount - keep connection alive
    };
  }, []);

  return connectedSocket;
}
```

- [ ] **步骤 2：Commit**

```bash
git add .
git commit -m "feat: add Socket.io client hook"
```

---

## 任务 16：Vercel 部署配置

**文件：**
- 修改：`package.json`, `.env.example`

- [ ] **步骤 1：更新 package.json scripts**

```json
{
  "scripts": {
    "dev": "next dev",
    "dev:socket": "npx ts-node --project tsconfig.server.json server/socket.ts",
    "build": "next build",
    "start": "next start",
    "postinstall": "prisma generate"
  }
}
```

- [ ] **步骤 2：更新 .env.example**

```env
# Database
DATABASE_URL="postgresql://user:password@localhost:5432/pixel_canvas"
REDIS_URL="redis://localhost:6379"

# Auth
GITHUB_CLIENT_ID=""
GITHUB_CLIENT_SECRET=""
GITHUB_CALLBACK_URL=""

# Admin
ADMIN_PASSWORD=""
NEXT_PUBLIC_ADMIN_PASSWORD=""

# Socket.io
SOCKET_PORT=3001
NEXT_PUBLIC_SOCKET_URL=""

# App
NEXT_PUBLIC_APP_URL=""

# AdSense
NEXT_PUBLIC_ADSENSE_CLIENT=""

# Vercel
VERCEL_URL=""
```

- [ ] **步骤 3：Commit**

```bash
git add .
git commit -m "feat: add deployment configuration for Vercel"
```

---

## 规格覆盖度自检

| 规格需求 | 对应任务 |
|---------|---------|
| 2000×2000 画布 | 任务 9 |
| 缩放/平移/点击 | 任务 9 |
| 32 色调色板 | 任务 9 |
| 像素信息浮窗 | 任务 9 |
| 点数系统（5分钟/点） | 任务 8, 11 |
| 点数上限 12 | 任务 11 |
| 心跳防作弊 | 任务 8 |
| 实时聊天室 | 任务 10 |
| 聊天防刷屏 | 任务 8 |
| 匿名游客 | 任务 6 |
| GitHub OAuth | 任务 6 |
| 多语言 12 种 | 任务 4, 5 |
| 语言切换器 | 任务 5 |
| 后台管理 | 任务 14 |
| 画布重置 | 任务 14 |
| 像素回滚 | 任务 14 |
| 广告位 | 任务 12, 13 |
| WebSocket 实时 | 任务 8, 15 |
| Redis 缓存 | 任务 2, 7 |
| PostgreSQL 持久化 | 任务 2, 7 |
| Vercel 部署 | 任务 16 |

所有规格需求均已覆盖，无遗漏。

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-05-25-pixel-canvas.md`。两种执行方式：

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** - 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？
