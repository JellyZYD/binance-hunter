'use client';

import { useState, useCallback, useRef } from 'react';
import { CANVAS_SIZE } from '@/types';

interface CanvasState {
  scale: number;
  offsetX: number;
  offsetY: number;
  selectedColor: string;
}

export function useCanvas() {
  const [state, setState] = useState<CanvasState>({
    scale: 0.5,
    offsetX: 0,
    offsetY: 0,
    selectedColor: '#000000',
  });

  const canvasRef = useRef<HTMLCanvasElement>(null);

  const zoom = useCallback(
    (delta: number, centerX: number, centerY: number) => {
      setState((prev) => {
        const factor = delta > 0 ? 0.9 : 1.1;
        const newScale = Math.max(0.1, Math.min(20, prev.scale * factor));
        const scaleChange = newScale / prev.scale;

        return {
          ...prev,
          scale: newScale,
          offsetX: centerX - (centerX - prev.offsetX) * scaleChange,
          offsetY: centerY - (centerY - prev.offsetY) * scaleChange,
        };
      });
    },
    []
  );

  const pan = useCallback((dx: number, dy: number) => {
    setState((prev) => ({
      ...prev,
      offsetX: prev.offsetX + dx,
      offsetY: prev.offsetY + dy,
    }));
  }, []);

  const getPixelCoord = useCallback(
    (clientX: number, clientY: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return null;

      const rect = canvas.getBoundingClientRect();
      const x = Math.floor(
        (clientX - rect.left - state.offsetX) / state.scale
      );
      const y = Math.floor(
        (clientY - rect.top - state.offsetY) / state.scale
      );

      if (x >= 0 && x < CANVAS_SIZE && y >= 0 && y < CANVAS_SIZE) {
        return { x, y };
      }
      return null;
    },
    [state.offsetX, state.offsetY, state.scale]
  );

  const setSelectedColor = useCallback((color: string) => {
    setState((prev) => ({ ...prev, selectedColor: color }));
  }, []);

  const resetView = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    setState((prev) => ({
      ...prev,
      scale: Math.min(canvas.width, canvas.height) / CANVAS_SIZE,
      offsetX: 0,
      offsetY: 0,
    }));
  }, []);

  return {
    ...state,
    canvasRef,
    zoom,
    pan,
    getPixelCoord,
    setSelectedColor,
    resetView,
  };
}
