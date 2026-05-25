import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';
import { CANVAS_SIZE } from '@/types';
import { getOrCreateUser } from '@/lib/auth';

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
    return NextResponse.json(
      { error: 'Not enough points', points },
      { status: 400 }
    );
  }

  const timestamp = Date.now();
  const nicknameCookie = req.cookies.get('nickname')?.value;
  const nickname = nicknameCookie
    ? decodeURIComponent(nicknameCookie)
    : `Guest_${userId.slice(0, 4)}`;

  // Deduct point
  const newPoints = await redis.decr(`user:points:${userId}`);
  if (newPoints < 0) {
    await redis.incr(`user:points:${userId}`);
    return NextResponse.json(
      { error: 'Not enough points', points: 0 },
      { status: 400 }
    );
  }

  // Save pixel
  await redis.hset('canvas:pixels', `${x},${y}`, color);
  await redis.hset(
    'canvas:pixel_meta',
    `${x},${y}`,
    JSON.stringify({ x, y, color, userId, nickname, timestamp })
  );

  // Save to history
  await redis.lpush(
    'canvas:history',
    JSON.stringify({ x, y, color, userId, nickname, timestamp })
  );
  await redis.ltrim('canvas:history', 0, 999);

  // Save to PostgreSQL when available. Redis remains the source for live drawing.
  try {
    await getOrCreateUser(userId);
    await prisma.pixelHistory.create({
      data: { x, y, color, userId },
    });
    await prisma.user.update({
      where: { id: userId },
      data: { totalPixels: { increment: 1 } },
    });
  } catch (error) {
    console.error('Failed to persist pixel history', error);
  }

  return NextResponse.json({ success: true, points: newPoints });
}
