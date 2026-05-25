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
