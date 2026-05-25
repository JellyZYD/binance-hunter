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
