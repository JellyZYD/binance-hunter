import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';

export async function GET(req: NextRequest) {
  const password = req.headers.get('x-admin-password');
  if (password !== process.env.ADMIN_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const searchParams = req.nextUrl.searchParams;
  const page = parseInt(searchParams.get('page') || '1');
  const limit = parseInt(searchParams.get('limit') || '50');
  const start = (page - 1) * limit;

  const messages = await redis.lrange('chat:messages', start, start + limit - 1);
  const total = await redis.llen('chat:messages');
  const parsed = messages.map((m) => JSON.parse(m));

  return NextResponse.json({
    messages: parsed,
    total,
    page,
    limit,
  });
}

export async function DELETE(req: NextRequest) {
  const { password, messageId, clearAll } = await req.json();

  if (password !== process.env.ADMIN_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  if (clearAll) {
    await redis.del('chat:messages');
    return NextResponse.json({ success: true, cleared: true });
  }

  if (messageId) {
    // Remove specific message by content matching
    const messages = await redis.lrange('chat:messages', 0, -1);
    for (const msg of messages) {
      const parsed = JSON.parse(msg);
      if (parsed.id === messageId) {
        await redis.lrem('chat:messages', 1, msg);
        break;
      }
    }
    return NextResponse.json({ success: true, deleted: messageId });
  }

  return NextResponse.json({ error: 'Missing messageId or clearAll' }, { status: 400 });
}
