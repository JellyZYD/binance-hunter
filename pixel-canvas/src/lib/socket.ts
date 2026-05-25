'use client';

import { io, Socket } from 'socket.io-client';
import { useEffect, useState } from 'react';

let socket: Socket | null = null;

export function getSocket(userId: string, nickname: string): Socket {
  if (!socket) {
    socket = io(
      process.env.NEXT_PUBLIC_SOCKET_URL || 'http://localhost:3001',
      {
        auth: { userId, nickname },
        autoConnect: true,
      }
    );
  }
  return socket;
}

export function useSocket() {
  const [connectedSocket, setConnectedSocket] = useState<Socket | null>(null);

  useEffect(() => {
    // Get user ID from localStorage
    let userId = localStorage.getItem('userId');
    if (!userId) {
      userId = crypto.randomUUID();
      localStorage.setItem('userId', userId);
    }

    const nickname =
      localStorage.getItem('nickname') ||
      `Guest_${Math.floor(Math.random() * 9000) + 1000}`;

    const s = getSocket(userId, nickname);
    setConnectedSocket(s);

    return () => {
      // Don't disconnect on unmount - keep connection alive
    };
  }, []);

  return connectedSocket;
}
