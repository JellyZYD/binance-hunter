import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';
import { CANVAS_SIZE } from '@/types';

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
  await redis.lpush(
    'canvas:history',
    JSON.stringify({ x, y, color, userId, timestamp: Date.now() })
  );
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
