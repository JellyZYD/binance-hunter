# 注册功能实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为像素画布添加用户名+邮箱+密码注册/登录系统，支持匿名数据迁移

**架构：** JWT cookie 认证（httpOnly，7天过期），bcrypt 密码加密，弹窗式登录/注册 UI，注册时自动继承匿名用户的点数和像素历史

**技术栈：** bcryptjs、jose（JWT）、Prisma、Next.js App Router、next-intl

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `prisma/schema.prisma` | 新增 email/password 字段 |
| `src/lib/auth.ts` | 新增密码哈希、JWT、认证函数 |
| `src/app/api/auth/register/route.ts` | 注册 API |
| `src/app/api/auth/login/route.ts` | 登录 API |
| `src/app/api/auth/logout/route.ts` | 退出登录 API |
| `src/app/api/auth/me/route.ts` | 获取当前用户 API |
| `src/hooks/useAuth.ts` | 客户端认证状态管理 |
| `src/components/auth/AuthModal.tsx` | 注册/登录弹窗 |
| `src/components/auth/UserMenu.tsx` | 顶栏用户菜单 |
| `src/app/[locale]/page.tsx` | 集成 UserMenu |
| `messages/*.json` | 12个语言文件新增 auth 命名空间 |

---

### 任务 1：安装依赖 + 数据库迁移

**文件：**
- 修改：`package.json`
- 修改：`prisma/schema.prisma`

- [ ] **步骤 1：安装 bcryptjs 和 jose**

```bash
cd E:\workshop\pixel-canvas
npm install bcryptjs jose
npm install -D @types/bcryptjs
```

- [ ] **步骤 2：更新 Prisma schema**

在 `prisma/schema.prisma` 的 User 模型中添加 email 和 password 字段：

```prisma
model User {
  id           String   @id @default(uuid())
  nickname     String   @db.VarChar(50)
  email        String?  @unique @db.VarChar(255)
  password     String?  @db.VarChar(60)
  githubId     String?  @unique @map("github_id") @db.VarChar(50)
  totalPixels  Int      @default(0) @map("total_pixels")
  createdAt    DateTime @default(now()) @map("created_at")
  pixels       PixelHistory[]

  @@map("users")
}
```

- [ ] **步骤 3：运行数据库迁移**

```bash
cd E:\workshop\pixel-canvas
npx prisma migrate dev --name add-email-password
```

预期：迁移成功，users 表新增 email 和 password 列

- [ ] **步骤 4：Commit**

```bash
git add package.json package-lock.json prisma/schema.prisma prisma/migrations/
git commit -m "feat: add email/password fields to User model"
```

---

### 任务 2：认证工具函数

**文件：**
- 修改：`src/lib/auth.ts`

- [ ] **步骤 1：在 auth.ts 中添加密码哈希和 JWT 函数**

在现有代码末尾追加：

```typescript
import { SignJWT, jwtVerify } from 'jose';
import { compare, hash } from 'bcryptjs';
import { cookies } from 'next/headers';

const JWT_SECRET = new TextEncoder().encode(
  process.env.JWT_SECRET || 'pixel-canvas-jwt-secret-change-in-production'
);
const COOKIE_NAME = 'auth_token';
const COOKIE_MAX_AGE = 7 * 24 * 60 * 60; // 7 days

export async function hashPassword(password: string): Promise<string> {
  return hash(password, 10);
}

export async function verifyPassword(
  password: string,
  hashed: string
): Promise<boolean> {
  return compare(password, hashed);
}

export async function createToken(userId: string): Promise<string> {
  return new SignJWT({ userId })
    .setProtectedHeader({ alg: 'HS256' })
    .setExpirationTime('7d')
    .sign(JWT_SECRET);
}

export async function verifyToken(
  token: string
): Promise<{ userId: string } | null> {
  try {
    const { payload } = await jwtVerify(token, JWT_SECRET);
    return { userId: payload.userId as string };
  } catch {
    return null;
  }
}

export async function setAuthCookie(userId: string) {
  const token = await createToken(userId);
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge: COOKIE_MAX_AGE,
    path: '/',
  });
}

export async function removeAuthCookie() {
  const cookieStore = await cookies();
  cookieStore.delete(COOKIE_NAME);
}

export async function getUserFromCookie(): Promise<string | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  if (!token) return null;
  const result = await verifyToken(token);
  return result?.userId ?? null;
}

export async function findUserByEmailOrUsername(login: string) {
  return prisma.user.findFirst({
    where: {
      OR: [{ email: login }, { nickname: login }],
    },
  });
}
```

- [ ] **步骤 2：类型检查**

```bash
cd E:\workshop\pixel-canvas
npx tsc --noEmit
```

预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add src/lib/auth.ts
git commit -m "feat: add password hashing and JWT auth functions"
```

---

### 任务 3：注册 API

**文件：**
- 创建：`src/app/api/auth/register/route.ts`

- [ ] **步骤 1：创建注册 API**

```typescript
import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';
import { hashPassword, setAuthCookie } from '@/lib/auth';
import { redis } from '@/lib/redis';

export async function POST(req: NextRequest) {
  const { username, email, password, anonymousId } = await req.json();

  // Validate
  if (!username || username.length < 2 || username.length > 20) {
    return NextResponse.json(
      { error: 'Username must be 2-20 characters' },
      { status: 400 }
    );
  }
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return NextResponse.json({ error: 'Invalid email' }, { status: 400 });
  }
  if (!password || password.length < 6) {
    return NextResponse.json(
      { error: 'Password must be 6+ characters' },
      { status: 400 }
    );
  }

  // Check uniqueness
  const existingUser = await prisma.user.findFirst({
    where: {
      OR: [{ nickname: username }, { email }],
    },
  });

  if (existingUser) {
    if (existingUser.email === email) {
      return NextResponse.json({ error: 'Email already exists' }, { status: 409 });
    }
    return NextResponse.json(
      { error: 'Username already exists' },
      { status: 409 }
    );
  }

  const hashed = await hashPassword(password);

  // Create user
  const user = await prisma.user.create({
    data: {
      nickname: username,
      email,
      password: hashed,
    },
  });

  // Migrate anonymous data if anonymousId provided
  if (anonymousId) {
    const oldPoints = await redis.get(`user:points:${anonymousId}`);
    if (oldPoints) {
      await redis.set(`user:points:${user.id}`, oldPoints);
    }

    const oldActiveMs = await redis.get(`user:active_ms:${anonymousId}`);
    if (oldActiveMs) {
      await redis.set(`user:active_ms:${user.id}`, oldActiveMs);
    }

    const oldLastGrant = await redis.get(`user:last_grant:${anonymousId}`);
    if (oldLastGrant) {
      await redis.set(`user:last_grant:${user.id}`, oldLastGrant);
    }

    // Migrate pixel history
    await prisma.pixelHistory.updateMany({
      where: { userId: anonymousId },
      data: { userId: user.id },
    });
  }

  await setAuthCookie(user.id);

  return NextResponse.json({
    user: { id: user.id, nickname: user.nickname },
  });
}
```

- [ ] **步骤 2：类型检查**

```bash
npx tsc --noEmit
```

- [ ] **步骤 3：Commit**

```bash
git add src/app/api/auth/register/route.ts
git commit -m "feat: add registration API with anonymous data migration"
```

---

### 任务 4：登录 API

**文件：**
- 创建：`src/app/api/auth/login/route.ts`

- [ ] **步骤 1：创建登录 API**

```typescript
import { NextRequest, NextResponse } from 'next/server';
import { findUserByEmailOrUsername, verifyPassword, setAuthCookie } from '@/lib/auth';

export async function POST(req: NextRequest) {
  const { login, password } = await req.json();

  if (!login || !password) {
    return NextResponse.json(
      { error: 'Login and password required' },
      { status: 400 }
    );
  }

  const user = await findUserByEmailOrUsername(login);
  if (!user || !user.password) {
    return NextResponse.json(
      { error: 'Invalid credentials' },
      { status: 401 }
    );
  }

  const valid = await verifyPassword(password, user.password);
  if (!valid) {
    return NextResponse.json(
      { error: 'Invalid credentials' },
      { status: 401 }
    );
  }

  await setAuthCookie(user.id);

  return NextResponse.json({
    user: { id: user.id, nickname: user.nickname },
  });
}
```

- [ ] **步骤 2：类型检查 + Commit**

```bash
npx tsc --noEmit
git add src/app/api/auth/login/route.ts
git commit -m "feat: add login API"
```

---

### 任务 5：退出登录 + 获取用户 API

**文件：**
- 创建：`src/app/api/auth/logout/route.ts`
- 创建：`src/app/api/auth/me/route.ts`

- [ ] **步骤 1：创建退出登录 API**

```typescript
import { NextResponse } from 'next/server';
import { removeAuthCookie } from '@/lib/auth';

export async function POST() {
  await removeAuthCookie();
  return NextResponse.json({ success: true });
}
```

- [ ] **步骤 2：创建获取用户 API**

```typescript
import { NextResponse } from 'next/server';
import { getUserFromCookie } from '@/lib/auth';
import { prisma } from '@/lib/db';

export async function GET() {
  const userId = await getUserFromCookie();
  if (!userId) {
    return NextResponse.json({ user: null });
  }

  const user = await prisma.user.findUnique({
    where: { id: userId },
    select: { id: true, nickname: true },
  });

  return NextResponse.json({ user });
}
```

- [ ] **步骤 3：类型检查 + Commit**

```bash
npx tsc --noEmit
git add src/app/api/auth/logout/route.ts src/app/api/auth/me/route.ts
git commit -m "feat: add logout and me APIs"
```

---

### 任务 6：useAuth Hook

**文件：**
- 创建：`src/hooks/useAuth.ts`

- [ ] **步骤 1：创建 useAuth hook**

```typescript
'use client';

import { useState, useEffect, useCallback } from 'react';

interface User {
  id: string;
  nickname: string;
}

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchUser = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/me');
      if (res.ok) {
        const data = await res.json();
        setUser(data.user);
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUser();
  }, [fetchUser]);

  const register = async (
    username: string,
    email: string,
    password: string
  ) => {
    const anonymousId = localStorage.getItem('userId');
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, email, password, anonymousId }),
    });

    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error);
    }

    const data = await res.json();
    localStorage.setItem('userId', data.user.id);
    localStorage.setItem('nickname', data.user.nickname);
    document.cookie = `userId=${data.user.id};path=/;max-age=31536000`;
    document.cookie = `nickname=${encodeURIComponent(data.user.nickname)};path=/;max-age=31536000`;
    setUser(data.user);
    return data.user;
  };

  const login = async (loginStr: string, password: string) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: loginStr, password }),
    });

    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error);
    }

    const data = await res.json();
    localStorage.setItem('userId', data.user.id);
    localStorage.setItem('nickname', data.user.nickname);
    document.cookie = `userId=${data.user.id};path=/;max-age=31536000`;
    document.cookie = `nickname=${encodeURIComponent(data.user.nickname)};path=/;max-age=31536000`;
    setUser(data.user);
    return data.user;
  };

  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    setUser(null);
  };

  return { user, loading, register, login, logout, refresh: fetchUser };
}
```

- [ ] **步骤 2：类型检查 + Commit**

```bash
npx tsc --noEmit
git add src/hooks/useAuth.ts
git commit -m "feat: add useAuth hook for client-side auth state"
```

---

### 任务 7：AuthModal 组件

**文件：**
- 创建：`src/components/auth/AuthModal.tsx`

- [ ] **步骤 1：创建 AuthModal 组件**

```tsx
'use client';

import { useState } from 'react';
import { useTranslations } from 'next-intl';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onAuth: () => void;
  mode: 'login' | 'register';
  onSwitchMode: (mode: 'login' | 'register') => void;
  onRegister: (username: string, email: string, password: string) => Promise<void>;
  onLogin: (login: string, password: string) => Promise<void>;
}

export default function AuthModal({
  isOpen,
  onClose,
  mode,
  onSwitchMode,
  onRegister,
  onLogin,
}: Props) {
  const t = useTranslations('auth');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [login, setLogin] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  if (!isOpen) return null;

  const resetForm = () => {
    setUsername('');
    setEmail('');
    setPassword('');
    setConfirmPassword('');
    setLogin('');
    setError('');
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError(t('passwordMismatch'));
      return;
    }

    setSubmitting(true);
    try {
      await onRegister(username, email, password);
      resetForm();
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Unknown error';
      if (msg.includes('Username already exists')) {
        setError(t('usernameExists'));
      } else if (msg.includes('Email already exists')) {
        setError(t('emailExists'));
      } else {
        setError(msg);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    try {
      await onLogin(login, password);
      resetForm();
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Unknown error';
      if (msg.includes('Invalid credentials')) {
        setError(t('invalidCredentials'));
      } else {
        setError(msg);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          resetForm();
          onClose();
        }
      }}
    >
      <div className="bg-gray-800 rounded-2xl p-6 w-full max-w-md shadow-2xl border border-gray-700">
        {/* Tab Switcher */}
        <div className="flex mb-6 bg-gray-700 rounded-xl p-1">
          <button
            onClick={() => {
              resetForm();
              onSwitchMode('login');
            }}
            className={`flex-1 py-2 rounded-lg text-sm font-medium transition-colors ${
              mode === 'login'
                ? 'bg-gray-600 text-white'
                : 'text-gray-400 hover:text-white'
            }`}
          >
            {t('login')}
          </button>
          <button
            onClick={() => {
              resetForm();
              onSwitchMode('register');
            }}
            className={`flex-1 py-2 rounded-lg text-sm font-medium transition-colors ${
              mode === 'register'
                ? 'bg-gray-600 text-white'
                : 'text-gray-400 hover:text-white'
            }`}
          >
            {t('register')}
          </button>
        </div>

        {error && (
          <div className="mb-4 p-3 bg-red-900/50 border border-red-700 rounded-lg text-sm text-red-300">
            {error}
          </div>
        )}

        {mode === 'register' ? (
          <form onSubmit={handleRegister} className="space-y-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">
                {t('username')}
              </label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                minLength={2}
                maxLength={20}
                className="w-full bg-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">
                {t('email')}
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="w-full bg-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">
                {t('password')}
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={6}
                className="w-full bg-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">
                {t('confirmPassword')}
              </label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                minLength={6}
                className="w-full bg-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 py-2.5 rounded-lg text-sm font-medium transition-colors"
            >
              {submitting ? '...' : t('register')}
            </button>
          </form>
        ) : (
          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <label className="block text-sm text-gray-400 mb-1">
                {t('username')}
              </label>
              <input
                type="text"
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                required
                className="w-full bg-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">
                {t('password')}
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="w-full bg-gray-700 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 py-2.5 rounded-lg text-sm font-medium transition-colors"
            >
              {submitting ? '...' : t('login')}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
```

- [ ] **步骤 2：类型检查 + Commit**

```bash
npx tsc --noEmit
git add src/components/auth/AuthModal.tsx
git commit -m "feat: add AuthModal component with login/register forms"
```

---

### 任务 8：UserMenu 组件

**文件：**
- 创建：`src/components/auth/UserMenu.tsx`

- [ ] **步骤 1：创建 UserMenu 组件**

```tsx
'use client';

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { useAuth } from '@/hooks/useAuth';
import AuthModal from './AuthModal';

export default function UserMenu() {
  const t = useTranslations('auth');
  const { user, loading, register, login, logout } = useAuth();
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<'login' | 'register'>('login');
  const [menuOpen, setMenuOpen] = useState(false);

  if (loading) {
    return (
      <div className="w-8 h-8 rounded-full bg-gray-700 animate-pulse" />
    );
  }

  if (!user) {
    return (
      <>
        <button
          onClick={() => {
            setModalMode('login');
            setModalOpen(true);
          }}
          className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium transition-colors"
        >
          {t('login')}
        </button>
        <AuthModal
          isOpen={modalOpen}
          onClose={() => setModalOpen(false)}
          mode={modalMode}
          onSwitchMode={setModalMode}
          onRegister={register}
          onLogin={login}
          onAuth={() => {}}
        />
      </>
    );
  }

  const initial = user.nickname.charAt(0).toUpperCase();

  return (
    <div className="relative">
      <button
        onClick={() => setMenuOpen(!menuOpen)}
        className="flex items-center gap-2 hover:bg-gray-700 rounded-lg px-2 py-1 transition-colors"
      >
        <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-sm font-bold">
          {initial}
        </div>
        <span className="text-sm font-medium">{user.nickname}</span>
      </button>

      {menuOpen && (
        <>
          <div
            className="fixed inset-0 z-40"
            onClick={() => setMenuOpen(false)}
          />
          <div className="absolute right-0 mt-2 w-48 bg-gray-800 rounded-xl shadow-2xl border border-gray-700 z-50 overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-700">
              <p className="text-sm font-medium">{user.nickname}</p>
              <p className="text-xs text-gray-400">ID: {user.id.slice(0, 8)}...</p>
            </div>
            <button
              onClick={() => {
                setMenuOpen(false);
                logout();
              }}
              className="w-full text-left px-4 py-2.5 text-sm text-red-400 hover:bg-gray-700 transition-colors"
            >
              {t('logout')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **步骤 2：类型检查 + Commit**

```bash
npx tsc --noEmit
git add src/components/auth/UserMenu.tsx
git commit -m "feat: add UserMenu component with avatar dropdown"
```

---

### 任务 9：集成到页面

**文件：**
- 修改：`src/app/[locale]/page.tsx`

- [ ] **步骤 1：在 page.tsx 中集成 UserMenu**

在 header 区域的 LanguageSwitcher 左边添加 UserMenu：

```tsx
import dynamic from 'next/dynamic';
import LanguageSwitcher from '@/components/ui/LanguageSwitcher';
import AdBanner from '@/components/ui/AdBanner';
import UserMenu from '@/components/auth/UserMenu';

// ... 其他 dynamic imports 保持不变

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
          <UserMenu />
          <LanguageSwitcher />
        </div>
      </header>

      {/* ... 其余保持不变 */}
    </div>
  );
}
```

- [ ] **步骤 2：类型检查 + Commit**

```bash
npx tsc --noEmit
git add src/app/\[locale\]/page.tsx
git commit -m "feat: integrate UserMenu into page header"
```

---

### 任务 10：国际化翻译

**文件：**
- 修改：`messages/zh-CN.json`
- 修改：`messages/zh-TW.json`
- 修改：`messages/en.json`
- 修改：其余 9 个 messages/*.json 文件

- [ ] **步骤 1：在所有翻译文件中添加 auth 命名空间**

在每个 messages/*.json 文件末尾（最后一个 `}` 之前）添加 `auth` 部分。

`messages/zh-CN.json` 追加：
```json
  "auth": {
    "register": "注册",
    "login": "登录",
    "logout": "退出登录",
    "username": "用户名",
    "email": "邮箱",
    "password": "密码",
    "confirmPassword": "确认密码",
    "usernameExists": "用户名已存在",
    "emailExists": "邮箱已注册",
    "invalidCredentials": "用户名或密码错误",
    "passwordMismatch": "两次密码不一致"
  }
```

`messages/en.json` 追加：
```json
  "auth": {
    "register": "Register",
    "login": "Login",
    "logout": "Logout",
    "username": "Username",
    "email": "Email",
    "password": "Password",
    "confirmPassword": "Confirm Password",
    "usernameExists": "Username already exists",
    "emailExists": "Email already registered",
    "invalidCredentials": "Invalid username or password",
    "passwordMismatch": "Passwords do not match"
  }
```

其余语言文件参照同样格式添加对应翻译。

- [ ] **步骤 2：类型检查 + Commit**

```bash
npx tsc --noEmit
git add messages/
git commit -m "feat: add auth translations for all 12 languages"
```

---

### 任务 11：端到端测试

- [ ] **步骤 1：启动开发服务器并测试注册流程**

```bash
cd E:\workshop\pixel-canvas
npm run dev
npm run dev:socket
```

在浏览器中：
1. 打开 http://localhost:3000/zh-CN
2. 点击"登录"按钮 → 弹窗出现
3. 切换到"注册"tab
4. 填写用户名、邮箱、密码、确认密码
5. 点击注册 → 成功后顶栏显示头像+昵称
6. 刷新页面 → 仍然保持登录状态
7. 点击头像 → 下拉菜单 → 退出登录
8. 重新登录 → 使用用户名或邮箱都能登录

- [ ] **步骤 2：Commit**

```bash
git add -A
git commit -m "feat: complete registration and login system"
```
