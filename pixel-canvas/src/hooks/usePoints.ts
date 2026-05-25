'use client';

import { useEffect, useSyncExternalStore } from 'react';

interface PointsState {
  points: number;
  nextPointIn: number;
  maxPoints: number;
  pointInterval: number;
  loaded: boolean;
}

interface PointsResponse {
  points: number;
  nextPointIn: number;
  maxPoints?: number;
  pointInterval?: number;
}

const HEARTBEAT_MS = 5000;

let state: PointsState = {
  points: 0,
  nextPointIn: 300000,
  maxPoints: 100,
  pointInterval: 300000,
  loaded: false,
};

let started = false;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let countdownTimer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function emit(nextState: Partial<PointsState>) {
  state = { ...state, ...nextState };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return state;
}

function ensureUserSession() {
  let userId = localStorage.getItem('userId');
  if (!userId) {
    userId = crypto.randomUUID();
    localStorage.setItem('userId', userId);
  }

  let nickname = localStorage.getItem('nickname');
  if (!nickname) {
    nickname = `Guest_${Math.floor(Math.random() * 9000) + 1000}`;
    localStorage.setItem('nickname', nickname);
  }

  document.cookie = `userId=${userId};path=/;max-age=31536000;samesite=lax`;
  document.cookie = `nickname=${encodeURIComponent(
    nickname
  )};path=/;max-age=31536000;samesite=lax`;
}

function applyServerState(data: PointsResponse) {
  const serverMax = data.maxPoints ?? 100;
  const serverInterval = data.pointInterval ?? 300000;
  emit({
    points: Math.max(0, Math.min(serverMax, data.points)),
    nextPointIn: Math.max(0, data.nextPointIn),
    maxPoints: serverMax,
    pointInterval: serverInterval,
    loaded: true,
  });
}

async function syncPoints(method: 'GET' | 'POST' = 'GET') {
  ensureUserSession();

  const res = await fetch('/api/points', {
    method,
    cache: 'no-store',
  });

  if (!res.ok) return;
  applyServerState(await res.json());
}

function startPointsStore() {
  if (started) return;
  started = true;

  void syncPoints();

  if (heartbeatTimer === null) {
    heartbeatTimer = setInterval(() => {
      void syncPoints('POST');
    }, HEARTBEAT_MS);
  }

  if (countdownTimer === null) {
    countdownTimer = setInterval(() => {
      if (!state.loaded || state.points >= state.maxPoints) return;

      const nextPointIn = Math.max(0, state.nextPointIn - 1000);
      emit({ nextPointIn });

      if (nextPointIn === 0) {
        void syncPoints('POST');
      }
    }, 1000);
  }

  window.addEventListener('focus', () => {
    void syncPoints('POST');
  });

  window.addEventListener('pagehide', () => {
    navigator.sendBeacon?.('/api/points');
  });
}

export function usePoints() {
  const pointsState = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    startPointsStore();
  }, []);

  const deductPoint = (serverPoints?: number) => {
    emit({
      points:
        typeof serverPoints === 'number'
          ? Math.max(0, serverPoints)
          : Math.max(0, state.points - 1),
    });
  };

  return {
    ...pointsState,
    deductPoint,
    refreshPoints: () => syncPoints(),
  };
}
