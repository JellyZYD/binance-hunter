import { NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { prisma } from '@/lib/db';

export async function GET() {
  const onlineUsers = await redis.scard('online:users');
  const totalPixels = await prisma.pixelHistory.count();

  return NextResponse.json({ onlineUsers, totalPixels });
}
