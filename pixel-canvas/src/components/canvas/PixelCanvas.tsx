'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { useCanvas } from '@/hooks/useCanvas';
import { useSocket } from '@/lib/socket';
import ColorPalette from './ColorPalette';
import PixelInfo from './PixelInfo';
import { CANVAS_SIZE } from '@/types';
import { useTranslations } from 'next-intl';

interface PixelUpdate {
  x: number;
  y: number;
  color: string;
}

export default function PixelCanvas() {
  const t = useTranslations('canvas');
  const {
    scale,
    offsetX,
    offsetY,
    selectedColor,
    canvasRef,
    zoom,
    pan,
    getPixelCoord,
    setSelectedColor,
    resetView,
  } = useCanvas();

  const socket = useSocket();
  const [pixelInfo, setPixelInfo] = useState<{ x: number; y: number } | null>(
    null
  );
  const [points, setPoints] = useState(0);
  const pixelsRef = useRef<Map<string, string>>(new Map());

  // Load initial canvas data
  useEffect(() => {
    async function loadChunks() {
      const chunks = 20; // 2000 / 100
      for (let cx = 0; cx < chunks; cx++) {
        for (let cy = 0; cy < chunks; cy++) {
          const res = await fetch(`/api/canvas?cx=${cx}&cy=${cy}`);
          const data = await res.json();
          Object.entries(data.pixels).forEach(([key, color]) => {
            pixelsRef.current.set(key, color as string);
          });
        }
      }
    }
    loadChunks();
  }, []);

  // Listen for real-time updates
  useEffect(() => {
    if (!socket) return;

    socket.on('pixel:update', (data: PixelUpdate) => {
      pixelsRef.current.set(`${data.x},${data.y}`, data.color);
    });

    socket.on('points:update', (newPoints: number) => {
      setPoints(newPoints);
    });

    return () => {
      socket.off('pixel:update');
      socket.off('points:update');
    };
  }, [socket]);

  // Render loop
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationId: number;

    function render() {
      if (!canvas || !ctx) return;

      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.save();
      ctx.translate(offsetX, offsetY);
      ctx.scale(scale, scale);

      // Draw pixels
      pixelsRef.current.forEach((color, key) => {
        const [x, y] = key.split(',').map(Number);
        ctx.fillStyle = color;
        ctx.fillRect(x, y, 1, 1);
      });

      ctx.restore();
      animationId = requestAnimationFrame(render);
    }

    render();
    return () => cancelAnimationFrame(animationId);
  }, [canvasRef, scale, offsetX, offsetY]);

  // Handle mouse wheel zoom
  const handleWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
      zoom(e.deltaY, e.clientX, e.clientY);
    },
    [zoom]
  );

  // Handle mouse click
  const handleClick = useCallback(
    (e: React.MouseEvent) => {
      const pos = getPixelCoord(e.clientX, e.clientY);
      if (pos) {
        setPixelInfo(pos);
      }
    },
    [getPixelCoord]
  );

  // Handle pixel placement
  const handlePlacePixel = useCallback(async () => {
    if (!pixelInfo || points < 1) return;

    const { x, y } = pixelInfo;
    const color = selectedColor;

    // Optimistic update
    pixelsRef.current.set(`${x},${y}`, color);

    // Send to server
    const res = await fetch('/api/pixel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y, color }),
    });

    if (res.ok) {
      const data = await res.json();
      setPoints(data.points);

      // Broadcast to others
      socket?.emit('pixel:update', { x, y, color });
    } else {
      // Revert on failure
      pixelsRef.current.delete(`${x},${y}`);
    }

    setPixelInfo(null);
  }, [pixelInfo, points, selectedColor, socket]);

  return (
    <div className="relative flex-1 overflow-hidden bg-gray-950">
      {/* Canvas */}
      <canvas
        ref={canvasRef}
        width={typeof window !== 'undefined' ? window.innerWidth : 1920}
        height={typeof window !== 'undefined' ? window.innerHeight : 1080}
        onWheel={handleWheel}
        onClick={handleClick}
        className="cursor-crosshair"
      />

      {/* Color Palette */}
      <div className="absolute bottom-4 left-4">
        <ColorPalette
          selectedColor={selectedColor}
          onColorSelect={setSelectedColor}
        />
      </div>

      {/* Points Display */}
      <div className="absolute top-4 left-4 bg-gray-800 rounded-lg p-3">
        <p className="text-sm text-gray-400">{t('placePixel')}</p>
        <p className="text-2xl font-bold">{points}</p>
      </div>

      {/* Place Button */}
      {pixelInfo && (
        <button
          onClick={handlePlacePixel}
          disabled={points < 1}
          className="absolute bottom-20 left-1/2 -translate-x-1/2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 px-6 py-3 rounded-full font-bold transition-colors"
        >
          {points < 1 ? t('noPoints') : t('placePixel')}
        </button>
      )}

      {/* Pixel Info */}
      {pixelInfo && (
        <PixelInfo
          x={pixelInfo.x}
          y={pixelInfo.y}
          onClose={() => setPixelInfo(null)}
        />
      )}

      {/* Zoom Controls */}
      <div className="absolute top-4 right-4 flex flex-col gap-2">
        <button
          onClick={() =>
            zoom(-1, window.innerWidth / 2, window.innerHeight / 2)
          }
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center"
        >
          +
        </button>
        <button
          onClick={() =>
            zoom(1, window.innerWidth / 2, window.innerHeight / 2)
          }
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center"
        >
          -
        </button>
        <button
          onClick={resetView}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center text-xs"
        >
          R
        </button>
      </div>
    </div>
  );
}
