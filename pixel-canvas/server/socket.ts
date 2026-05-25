import { Server } from 'socket.io';
import Redis from 'ioredis';

const redis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
const PORT = parseInt(process.env.SOCKET_PORT || '3001');

async function getConfig(key: string, fallback: string): Promise<string> {
  const val = await redis.get(key);
  return val || fallback;
}

const io = new Server(PORT, {
  cors: {
    origin: process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000',
    methods: ['GET', 'POST'],
  },
});

// Track connected users
const userSockets = new Map<string, string>();

io.on('connection', (socket) => {
  const userId = socket.handshake.auth.userId as string;
  if (!userId) {
    socket.disconnect();
    return;
  }

  // Register user
  userSockets.set(userId, socket.id);
  redis.sadd('online:users', userId);

  // Broadcast online count
  redis.scard('online:users').then((count) => {
    io.emit('user:count', count);
  });

  // Send current points (give initial points to new users)
  redis.get(`user:points:${userId}`).then(async (points) => {
    const initialPoints = parseInt(await getConfig('config:initial_points', '100'));
    if (!points) {
      const now = Date.now();
      await redis.set(`user:points:${userId}`, initialPoints.toString());
      await redis.set(`user:last_grant:${userId}`, now.toString());
      socket.emit('points:update', { points: initialPoints, lastGrant: now });
    } else {
      const lastGrant = await redis.get(`user:last_grant:${userId}`);
      socket.emit('points:update', {
        points: parseInt(points),
        lastGrant: lastGrant ? parseInt(lastGrant) : Date.now(),
      });
    }
  });

  // Start heartbeat check
  redis.set(`user:heartbeat:${userId}`, Date.now().toString(), 'EX', 60);

  // Handle heartbeat
  socket.on('heartbeat', async () => {
    const lastHeartbeat = await redis.get(`user:heartbeat:${userId}`);
    const now = Date.now();

    const maxPoints = parseInt(await getConfig('config:max_points', '100'));
    const pointInterval = parseInt(await getConfig('config:point_interval_ms', '300000'));

    if (lastHeartbeat) {
      const elapsed = now - parseInt(lastHeartbeat);
      // If more than 30 seconds since last heartbeat, check for point grant
      if (elapsed >= 30000) {
        const currentPoints = parseInt(
          (await redis.get(`user:points:${userId}`)) || '0'
        );
        if (currentPoints < maxPoints) {
          // Check if configured interval has passed since last point grant
          const lastGrant = await redis.get(`user:last_grant:${userId}`);
          if (!lastGrant || now - parseInt(lastGrant) >= pointInterval) {
            await redis.incr(`user:points:${userId}`);
            await redis.set(`user:last_grant:${userId}`, now.toString());
            const newPoints = parseInt(
              (await redis.get(`user:points:${userId}`)) || '0'
            );
            socket.emit('points:update', { points: newPoints, lastGrant: now });
          }
        }
      }
    }

    await redis.set(`user:heartbeat:${userId}`, now.toString(), 'EX', 60);
  });

  // Handle pixel placement broadcast
  socket.on('pixel:update', (data) => {
    socket.broadcast.emit('pixel:update', data);
  });

  // Handle chat messages
  socket.on('chat:message', async (content: string) => {
    if (
      typeof content !== 'string' ||
      content.length > 200 ||
      content.trim().length === 0
    ) {
      return;
    }

    // Rate limit: 5 seconds between messages
    const lastMessage = await redis.get(`user:chat_cooldown:${userId}`);
    const now = Date.now();
    if (lastMessage && now - parseInt(lastMessage) < 5000) {
      socket.emit(
        'chat:cooldown',
        Math.ceil((5000 - (now - parseInt(lastMessage))) / 1000)
      );
      return;
    }

    const nickname =
      (socket.handshake.auth.nickname as string) || 'Anonymous';
    const message = {
      id: `${userId}-${now}`,
      userId,
      nickname,
      content: content.trim(),
      timestamp: now,
    };

    // Save to Redis
    await redis.lpush('chat:messages', JSON.stringify(message));
    await redis.ltrim('chat:messages', 0, 49);
    await redis.set(`user:chat_cooldown:${userId}`, now.toString());

    // Broadcast
    io.emit('chat:message', message);
  });

  // Load chat history
  socket.on('chat:history', async () => {
    const messages = await redis.lrange('chat:messages', 0, 49);
    const parsed = messages.map((m) => JSON.parse(m)).reverse();
    socket.emit('chat:history', parsed);
  });

  // Handle disconnect
  socket.on('disconnect', async () => {
    userSockets.delete(userId);
    await redis.srem('online:users', userId);

    const count = await redis.scard('online:users');
    io.emit('user:count', count);
  });
});

console.log(`Socket.io server running on port ${PORT}`);
