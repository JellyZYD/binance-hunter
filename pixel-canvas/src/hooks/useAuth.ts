'use client';

import { useState, useEffect, useCallback } from 'react';
import { updateSocketAuth } from '@/lib/socket';

interface User {
  id: string;
  nickname: string;
}

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchUser = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/me');
      if (res.ok) {
        const data = await res.json();
        setUser(data.user);
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUser();
  }, [fetchUser]);

  const register = async (
    username: string,
    email: string,
    password: string
  ) => {
    const anonymousId = localStorage.getItem('userId');
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, email, password, anonymousId }),
    });

    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error);
    }

    const data = await res.json();
    localStorage.setItem('userId', data.user.id);
    localStorage.setItem('nickname', data.user.nickname);
    document.cookie = `userId=${data.user.id};path=/;max-age=31536000`;
    document.cookie = `nickname=${encodeURIComponent(data.user.nickname)};path=/;max-age=31536000`;
    updateSocketAuth(data.user.id, data.user.nickname);
    setUser(data.user);
    return data.user;
  };

  const login = async (loginStr: string, password: string) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ login: loginStr, password }),
    });

    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.error);
    }

    const data = await res.json();
    localStorage.setItem('userId', data.user.id);
    localStorage.setItem('nickname', data.user.nickname);
    document.cookie = `userId=${data.user.id};path=/;max-age=31536000`;
    document.cookie = `nickname=${encodeURIComponent(data.user.nickname)};path=/;max-age=31536000`;
    updateSocketAuth(data.user.id, data.user.nickname);
    setUser(data.user);
    return data.user;
  };

  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    const userId = localStorage.getItem('userId') || '';
    const guestNickname = `Guest_${Math.floor(Math.random() * 9000) + 1000}`;
    updateSocketAuth(userId, guestNickname);
    setUser(null);
  };

  return { user, loading, register, login, logout, refresh: fetchUser };
}
