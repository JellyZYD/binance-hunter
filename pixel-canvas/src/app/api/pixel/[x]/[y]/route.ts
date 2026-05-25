import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';
import { redis } from '@/lib/redis';

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ x: string; y: string }> }
) {
  const { x: xStr, y: yStr } = await params;
  const x = parseInt(xStr);
  const y = parseInt(yStr);

  // 1. Check Redis pixel_meta first (fastest)
  const meta = await redis.hget('canvas:pixel_meta', `${x},${y}`);
  if (meta) {
    try {
      return NextResponse.json(JSON.parse(meta));
    } catch {}
  }

  // 2. Check if pixel exists in canvas (color only)
  const color = await redis.hget('canvas:pixels', `${x},${y}`);
  if (!color) {
    return NextResponse.json({ error: 'Pixel not found' }, { status: 404 });
  }

  // 3. Try PostgreSQL for history (slower)
  try {
    const record = await prisma.pixelHistory.findFirst({
      where: { x, y },
      orderBy: { createdAt: 'desc' },
      include: { user: { select: { nickname: true } } },
    });

    if (record) {
      return NextResponse.json({
        x: record.x,
        y: record.y,
        color: record.color,
        nickname: record.user.nickname,
        timestamp: record.createdAt.getTime(),
      });
    }
  } catch (error) {
    console.error('Failed to read pixel history from database', error);
  }

  // 4. Fallback: pixel exists but no metadata
  return NextResponse.json({
    x,
    y,
    color,
    nickname: 'Unknown',
    timestamp: Date.now(),
  });
}
