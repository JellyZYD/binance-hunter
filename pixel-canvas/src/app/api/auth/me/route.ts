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
