import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';

export async function GET(req: NextRequest) {
  const password = req.headers.get('x-admin-password');
  if (password !== process.env.ADMIN_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const initialPoints = parseInt(
    (await redis.get('config:initial_points')) || '100'
  );
  const maxPoints = parseInt(
    (await redis.get('config:max_points')) || '100'
  );
  const pointInterval = parseInt(
    (await redis.get('config:point_interval_ms')) || '300000'
  );

  return NextResponse.json({
    initialPoints,
    maxPoints,
    pointInterval,
  });
}

export async function POST(req: NextRequest) {
  const { password, initialPoints, maxPoints, pointInterval } = await req.json();

  if (password !== process.env.ADMIN_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  if (typeof initialPoints === 'number' && initialPoints >= 0) {
    await redis.set('config:initial_points', initialPoints.toString());
  }
  if (typeof maxPoints === 'number' && maxPoints >= 1) {
    await redis.set('config:max_points', maxPoints.toString());
  }
  if (typeof pointInterval === 'number' && pointInterval >= 60000) {
    await redis.set('config:point_interval_ms', pointInterval.toString());
  }

  return NextResponse.json({ success: true });
}
