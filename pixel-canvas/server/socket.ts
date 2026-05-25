import { Server } from 'socket.io';
import Redis from 'ioredis';

const redis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
const PORT = parseInt(process.env.SOCKET_PORT || '3001');

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

  // Send current points
  redis.get(`user:points:${userId}`).then((points) => {
    socket.emit('points:update', parseInt(points || '0'));
  });

  // Start heartbeat check
  redis.set(`user:heartbeat:${userId}`, Date.now().toString(), 'EX', 60);

  // Handle heartbeat
  socket.on('heartbeat', async () => {
    const lastHeartbeat = await redis.get(`user:heartbeat:${userId}`);
    const now = Date.now();

    if (lastHeartbeat) {
      const elapsed = now - parseInt(lastHeartbeat);
      // If more than 30 seconds since last heartbeat, check for point grant
      if (elapsed >= 30000) {
        const currentPoints = parseInt(
          (await redis.get(`user:points:${userId}`)) || '0'
        );
        if (currentPoints < 12) {
          // Check if 5 minutes have passed since last point grant
          const lastGrant = await redis.get(`user:last_grant:${userId}`);
          if (!lastGrant || now - parseInt(lastGrant) >= 300000) {
            await redis.incr(`user:points:${userId}`);
            await redis.set(`user:last_grant:${userId}`, now.toString());
            const newPoints = parseInt(
              (await redis.get(`user:points:${userId}`)) || '0'
            );
            socket.emit('points:update', newPoints);
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
