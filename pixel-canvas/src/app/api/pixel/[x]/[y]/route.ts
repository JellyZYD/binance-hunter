import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ x: string; y: string }> }
) {
  const { x: xStr, y: yStr } = await params;
  const x = parseInt(xStr);
  const y = parseInt(yStr);

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
