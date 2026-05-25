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

  const getFitView = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return null;

    const padding = 32;
    const availableWidth = Math.max(1, canvas.width - padding * 2);
    const availableHeight = Math.max(1, canvas.height - padding * 2);
    const scale = Math.min(availableWidth, availableHeight) / CANVAS_SIZE;

    return {
      scale,
      offsetX: (canvas.width - CANVAS_SIZE * scale) / 2,
      offsetY: (canvas.height - CANVAS_SIZE * scale) / 2,
    };
  }, []);

  const zoom = useCallback(
    (delta: number, centerX: number, centerY: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;

      const rect = canvas.getBoundingClientRect();
      const localCenterX = centerX - rect.left;
      const localCenterY = centerY - rect.top;

      setState((prev) => {
        const factor = delta > 0 ? 0.9 : 1.1;
        const newScale = Math.max(0.1, Math.min(20, prev.scale * factor));
        const scaleChange = newScale / prev.scale;

        return {
          ...prev,
          scale: newScale,
          offsetX:
            localCenterX - (localCenterX - prev.offsetX) * scaleChange,
          offsetY:
            localCenterY - (localCenterY - prev.offsetY) * scaleChange,
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
    const view = getFitView();
    if (!view) return;

    setState((prev) => ({
      ...prev,
      ...view,
    }));
  }, [getFitView]);

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
