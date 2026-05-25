import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { HEARTBEAT_INTERVAL_MS } from '@/types';

const ACTIVE_MS_CAP = HEARTBEAT_INTERVAL_MS * 2 + 5000;

async function getConfig(key: string, fallback: number): Promise<number> {
  const val = await redis.get(key);
  return val ? parseInt(val, 10) : fallback;
}

function keys(userId: string) {
  return {
    points: `user:points:${userId}`,
    activeMs: `user:active_ms:${userId}`,
    lastSeen: `user:last_seen:${userId}`,
    lastGrant: `user:last_grant:${userId}`,
  };
}

async function readPointState(userId: string) {
  const maxPoints = await getConfig('config:max_points', 100);
  const pointInterval = await getConfig('config:point_interval_ms', 300000);
  const initialPoints = await getConfig('config:initial_points', 100);

  const userKeys = keys(userId);
  const [pointsValue, activeMsValue, lastGrantValue] =
    await redis.mget(
      userKeys.points,
      userKeys.activeMs,
      userKeys.lastGrant
    );

  let points = pointsValue === null ? initialPoints : parseInt(pointsValue, 10);
  if (!Number.isFinite(points)) points = 0;
  points = Math.max(0, Math.min(maxPoints, points));

  let activeMs = activeMsValue === null ? 0 : parseInt(activeMsValue, 10);
  if (!Number.isFinite(activeMs)) activeMs = 0;

  if (activeMsValue === null && lastGrantValue !== null && points < maxPoints) {
    const lastGrant = parseInt(lastGrantValue, 10);
    if (Number.isFinite(lastGrant)) {
      activeMs = Math.max(
        0,
        Math.min(pointInterval - 1, Date.now() - lastGrant)
      );
    }
  }

  if (points >= maxPoints) activeMs = 0;

  await redis
    .pipeline()
    .set(userKeys.points, points.toString())
    .set(userKeys.activeMs, activeMs.toString())
    .exec();

  return { points, activeMs, maxPoints, pointInterval };
}

function responseBody(points: number, activeMs: number, maxPoints: number, pointInterval: number) {
  return {
    points,
    maxPoints,
    pointInterval,
    nextPointIn:
      points >= maxPoints
        ? 0
        : Math.max(0, pointInterval - Math.min(activeMs, pointInterval)),
  };
}

export async function GET(req: NextRequest) {
  const userId = req.cookies.get('userId')?.value;
  if (!userId) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const { points, activeMs, maxPoints, pointInterval } = await readPointState(userId);
  await redis.set(keys(userId).lastSeen, Date.now().toString());

  return NextResponse.json(responseBody(points, activeMs, maxPoints, pointInterval));
}

export async function POST(req: NextRequest) {
  const userId = req.cookies.get('userId')?.value;
  if (!userId) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const userKeys = keys(userId);
  const now = Date.now();
  const [{ points: currentPoints, activeMs: currentActiveMs, maxPoints, pointInterval }, lastSeenValue] =
    await Promise.all([readPointState(userId), redis.get(userKeys.lastSeen)]);

  let points = currentPoints;
  let activeMs = currentActiveMs;

  if (points < maxPoints && lastSeenValue !== null) {
    const lastSeen = parseInt(lastSeenValue, 10);
    if (Number.isFinite(lastSeen)) {
      const elapsed = now - lastSeen;
      if (elapsed > 0) {
        activeMs += Math.min(elapsed, ACTIVE_MS_CAP);
      }
    }

    while (activeMs >= pointInterval && points < maxPoints) {
      points += 1;
      activeMs -= pointInterval;
    }
  }

  if (points >= maxPoints) activeMs = 0;

  await redis
    .pipeline()
    .set(userKeys.points, points.toString())
    .set(userKeys.activeMs, activeMs.toString())
    .set(userKeys.lastSeen, now.toString())
    .set(userKeys.lastGrant, (now - activeMs).toString())
    .exec();

  return NextResponse.json(responseBody(points, activeMs, maxPoints, pointInterval));
}
