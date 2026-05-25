'use client';

import { useState, useEffect, useCallback } from 'react';
import { useSocket } from '@/lib/socket';
import { ChatMessage } from '@/types';

export function useChat() {
  const socket = useSocket();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cooldown, setCooldown] = useState(0);
  const [onlineCount, setOnlineCount] = useState(0);

  useEffect(() => {
    if (!socket) return;

    // Load history
    socket.emit('chat:history');
    socket.on('chat:history', (history: ChatMessage[]) => {
      setMessages(history);
    });

    // Listen for new messages
    socket.on('chat:message', (message: ChatMessage) => {
      setMessages((prev) => [...prev.slice(-49), message]);
    });

    // Listen for online count
    socket.on('user:count', (count: number) => {
      setOnlineCount(count);
    });

    // Listen for cooldown
    socket.on('chat:cooldown', (seconds: number) => {
      setCooldown(seconds);
    });

    return () => {
      socket.off('chat:history');
      socket.off('chat:message');
      socket.off('user:count');
      socket.off('chat:cooldown');
    };
  }, [socket]);

  // Cooldown timer
  useEffect(() => {
    if (cooldown <= 0) return;

    const timer = setInterval(() => {
      setCooldown((prev) => Math.max(0, prev - 1));
    }, 1000);

    return () => clearInterval(timer);
  }, [cooldown]);

  const sendMessage = useCallback(
    (content: string) => {
      if (!socket || cooldown > 0) return;
      socket.emit('chat:message', content);
    },
    [socket, cooldown]
  );

  return { messages, sendMessage, cooldown, onlineCount };
}
