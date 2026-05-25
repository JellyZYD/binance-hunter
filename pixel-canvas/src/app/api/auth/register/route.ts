import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';
import { hashPassword, setAuthCookie } from '@/lib/auth';
import { redis } from '@/lib/redis';

export async function POST(req: NextRequest) {
  const { username, email, password, anonymousId } = await req.json();

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
