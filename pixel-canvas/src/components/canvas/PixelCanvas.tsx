'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { useCanvas } from '@/hooks/useCanvas';
import { usePoints } from '@/hooks/usePoints';
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
  const { points, deductPoint } = usePoints();
  const pixelsRef = useRef<Map<string, string>>(new Map());
  const hoveredRef = useRef<{ x: number; y: number } | null>(null);
  const hoverPosRef = useRef<{ left: number; top: number }>({ left: 0, top: 0 });
  const [hoveredPixel, setHoveredPixel] = useState<{
    x: number;
    y: number;
    left: number;
    top: number;
    color?: string;
  } | null>(null);

  // Drag state for panning
  const isDragging = useRef(false);
  const hasDragged = useRef(false);
  const lastMouse = useRef({ x: 0, y: 0 });
  const didFitInitialView = useRef(false);

  // Load initial canvas data (parallel, 10 at a time)
  useEffect(() => {
    async function loadChunks() {
      const chunks = 20;
      const urls: string[] = [];
      for (let cx = 0; cx < chunks; cx++) {
        for (let cy = 0; cy < chunks; cy++) {
          urls.push(`/api/canvas?cx=${cx}&cy=${cy}`);
        }
      }
      // Load 10 chunks in parallel
      for (let i = 0; i < urls.length; i += 10) {
        const batch = urls.slice(i, i + 10);
        const results = await Promise.all(
          batch.map((url) => fetch(url).then((r) => r.json()))
        );
        for (const data of results) {
          Object.entries(data.pixels).forEach(([key, color]) => {
            pixelsRef.current.set(key, color as string);
          });
        }
      }
    }
    loadChunks();
  }, []);

  // Listen for real-time pixel updates
  useEffect(() => {
    if (!socket) return;

    socket.on('pixel:update', (data: PixelUpdate) => {
      pixelsRef.current.set(`${data.x},${data.y}`, data.color);
    });

    return () => {
      socket.off('pixel:update');
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

      ctx.fillStyle = '#2f343d';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.save();
      ctx.translate(offsetX, offsetY);
      ctx.scale(scale, scale);

      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);
      ctx.lineWidth = 2 / scale;
      ctx.strokeStyle = '#111827';
      ctx.strokeRect(0, 0, CANVAS_SIZE, CANVAS_SIZE);

      pixelsRef.current.forEach((color, key) => {
        const [x, y] = key.split(',').map(Number);
        ctx.fillStyle = color;
        ctx.fillRect(x, y, 1, 1);
      });

      if (hoveredRef.current) {
        ctx.lineWidth = 1 / scale;
        ctx.strokeStyle = '#2563eb';
        ctx.strokeRect(hoveredRef.current.x, hoveredRef.current.y, 1, 1);
      }

      ctx.restore();
      animationId = requestAnimationFrame(render);
    }

    render();
    return () => cancelAnimationFrame(animationId);
  }, [canvasRef, scale, offsetX, offsetY]);

  // Keep the canvas buffer matched to the visible panel.
  useEffect(() => {
    const canvas = canvasRef.current;
    const container = canvas?.parentElement;
    if (!canvas || !container) return;

    function resize() {
      if (!canvas || !container) return;
      const rect = container.getBoundingClientRect();
      canvas.width = Math.max(1, Math.floor(rect.width));
      canvas.height = Math.max(1, Math.floor(rect.height));
      if (!didFitInitialView.current) {
        didFitInitialView.current = true;
        requestAnimationFrame(resetView);
      }
    }

    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    return () => observer.disconnect();
  }, [canvasRef, resetView]);

  // Fix passive wheel listener
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    function handleWheel(e: WheelEvent) {
      e.preventDefault();
      zoom(e.deltaY, e.clientX, e.clientY);
    }

    canvas.addEventListener('wheel', handleWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', handleWheel);
  }, [canvasRef, zoom]);

  // Mouse drag for panning
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button === 0) {
        isDragging.current = true;
        hasDragged.current = false;
        lastMouse.current = { x: e.clientX, y: e.clientY };
      }
    },
    []
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (isDragging.current) {
        const dx = e.clientX - lastMouse.current.x;
        const dy = e.clientY - lastMouse.current.y;
        if (Math.abs(dx) + Math.abs(dy) > 2) {
          hasDragged.current = true;
        }
        lastMouse.current = { x: e.clientX, y: e.clientY };
        pan(dx, dy);
        return;
      }

      const pos = getPixelCoord(e.clientX, e.clientY);
      const rect = e.currentTarget.getBoundingClientRect();
      hoverPosRef.current = {
        left: e.clientX - rect.left + 12,
        top: e.clientY - rect.top + 12,
      };
      if (pos && hoveredRef.current?.x === pos.x && hoveredRef.current?.y === pos.y) {
        return;
      }
      hoveredRef.current = pos;
      setHoveredPixel(
        pos
          ? {
              x: pos.x,
              y: pos.y,
              left: hoverPosRef.current.left,
              top: hoverPosRef.current.top,
              color: pixelsRef.current.get(`${pos.x},${pos.y}`),
            }
          : null
      );
    },
    [getPixelCoord, pan]
  );

  const handleMouseUp = useCallback(() => {
    isDragging.current = false;
  }, []);

  // Handle pixel placement
  const placePixel = useCallback(async (x: number, y: number) => {
    if (points < 1) return;

    const color = selectedColor;
    const key = `${x},${y}`;
    const previousColor = pixelsRef.current.get(key);

    // Optimistic update
    pixelsRef.current.set(key, color);
    deductPoint();

    // Send to server
    const res = await fetch('/api/pixel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y, color }),
    });

    if (res.ok) {
      const data = await res.json();
      // Sync with actual server points (replaces optimistic deduction)
      if (typeof data.points === 'number') {
        deductPoint(data.points);
      }
      // Broadcast to others
      socket?.emit('pixel:update', { x, y, color });
      setHoveredPixel((current) =>
        current?.x === x && current.y === y ? { ...current, color } : current
      );
    } else {
      // Revert on failure
      if (previousColor) {
        pixelsRef.current.set(key, previousColor);
      } else {
        pixelsRef.current.delete(key);
      }
      const data = await res.json().catch(() => null);
      if (typeof data?.points === 'number') {
        deductPoint(data.points);
      } else {
        deductPoint(points);
      }
    }
  }, [deductPoint, points, selectedColor, socket]);

  const handleClick = useCallback(
    (e: React.MouseEvent) => {
      if (hasDragged.current) return;
      const pos = getPixelCoord(e.clientX, e.clientY);
      if (pos) {
        void placePixel(pos.x, pos.y);
      }
    },
    [getPixelCoord, placePixel]
  );

  const zoomAtCenter = useCallback(
    (delta: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      zoom(delta, rect.left + rect.width / 2, rect.top + rect.height / 2);
    },
    [canvasRef, zoom]
  );

  return (
    <div className="absolute inset-0 overflow-hidden bg-[#2f343d]">
      {/* Canvas */}
      <canvas
        ref={canvasRef}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => {
          handleMouseUp();
          hoveredRef.current = null;
          setHoveredPixel(null);
        }}
        onClick={handleClick}
        className="block h-full w-full cursor-crosshair"
      />

      {/* Color Palette */}
      <div className="absolute bottom-4 left-4">
        <ColorPalette
          selectedColor={selectedColor}
          onColorSelect={setSelectedColor}
        />
      </div>

      {/* Points Display */}
      <div className="absolute top-4 left-4 bg-gray-800 rounded-lg p-3 text-white">
        <p className="text-sm text-gray-400">{t('placePixel')}</p>
        <p className="text-2xl font-bold">{points}</p>
      </div>

      {/* Pixel Info */}
      {hoveredPixel && (
        <PixelInfo
          x={hoveredPixel.x}
          y={hoveredPixel.y}
          left={hoveredPixel.left}
          top={hoveredPixel.top}
          color={hoveredPixel.color}
          onClose={() => setHoveredPixel(null)}
        />
      )}

      {/* Zoom Controls */}
      <div className="absolute top-4 right-4 flex flex-col gap-2">
        <button
          onClick={() => zoomAtCenter(-1)}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center text-white"
        >
          +
        </button>
        <button
          onClick={() => zoomAtCenter(1)}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center text-white"
        >
          -
        </button>
        <button
          onClick={resetView}
          className="bg-gray-800 hover:bg-gray-700 w-10 h-10 rounded-lg flex items-center justify-center text-xs text-white"
        >
          R
        </button>
      </div>
    </div>
  );
}
