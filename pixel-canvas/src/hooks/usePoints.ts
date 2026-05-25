'use client';

import { useState, useEffect, useRef } from 'react';
import { useSocket } from '@/lib/socket';
import { MAX_POINTS, POINT_INTERVAL_MS, HEARTBEAT_INTERVAL_MS } from '@/types';

export function usePoints() {
  const socket = useSocket();
  const [points, setPoints] = useState(0);
  const [nextPointIn, setNextPointIn] = useState(0);
  const lastGrantRef = useRef<number>(Date.now());
  const heartbeatRef = useRef<NodeJS.Timeout>();

  useEffect(() => {
    if (!socket) return;

    socket.on('points:update', (newPoints: number) => {
      setPoints(newPoints);
      lastGrantRef.current = Date.now();
    });

    // Start heartbeat
    heartbeatRef.current = setInterval(() => {
      socket.emit('heartbeat');
    }, HEARTBEAT_INTERVAL_MS);

    // Initial heartbeat
    socket.emit('heartbeat');

    return () => {
      socket.off('points:update');
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
      }
    };
  }, [socket]);

  // Countdown timer
  useEffect(() => {
    if (points >= MAX_POINTS) {
      setNextPointIn(0);
      return;
    }

    const timer = setInterval(() => {
      const elapsed = Date.now() - lastGrantRef.current;
      const remaining = Math.max(0, POINT_INTERVAL_MS - elapsed);
      setNextPointIn(remaining);
    }, 1000);

    return () => clearInterval(timer);
  }, [points]);

  // Pause on blur
  useEffect(() => {
    const handleBlur = () => {
      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
      }
    };

    const handleFocus = () => {
      if (socket) {
        heartbeatRef.current = setInterval(() => {
          socket.emit('heartbeat');
        }, HEARTBEAT_INTERVAL_MS);
        socket.emit('heartbeat');
      }
    };

    window.addEventListener('blur', handleBlur);
    window.addEventListener('focus', handleFocus);

    return () => {
      window.removeEventListener('blur', handleBlur);
      window.removeEventListener('focus', handleFocus);
    };
  }, [socket]);

  return { points, nextPointIn, maxPoints: MAX_POINTS };
}
