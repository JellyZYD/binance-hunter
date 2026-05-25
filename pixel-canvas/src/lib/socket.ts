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
        autoConnect: false,
      }
    );
  }
  return socket;
}

export function updateSocketAuth(userId: string, nickname: string) {
  if (socket) {
    socket.auth = { userId, nickname };
    socket.disconnect();
    socket.connect();
  }
}

export function useSocket() {
  const [connectedSocket, setConnectedSocket] = useState<Socket | null>(null);

  useEffect(() => {
    let userId = localStorage.getItem('userId');
    if (!userId) {
      userId = crypto.randomUUID();
      localStorage.setItem('userId', userId);
    }

    // Set cookie for API calls
    document.cookie = `userId=${userId};path=/;max-age=31536000`;

    const nickname =
      localStorage.getItem('nickname') ||
      `Guest_${Math.floor(Math.random() * 9000) + 1000}`;

    const s = getSocket(userId, nickname);

    // Attach points listener before connecting
    s.on('points:update', (data: { points: number; lastGrant: number }) => {
      // Store for other hooks to read via event
      window.dispatchEvent(
        new CustomEvent('points:update', { detail: data })
      );
    });

    // Now connect
    if (!s.connected) {
      s.connect();
    }

    setConnectedSocket(s);

    return () => {
      s.off('points:update');
    };
  }, []);

  return connectedSocket;
}
