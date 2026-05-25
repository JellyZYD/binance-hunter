'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useState } from 'react';

interface Props {
  x: number;
  y: number;
  left: number;
  top: number;
  color?: string;
  onClose: () => void;
}

interface PixelData {
  nickname: string;
  timestamp: number;
  color: string;
}

export default function PixelInfo({ x, y, left, top, color, onClose }: Props) {
  const t = useTranslations('pixelInfo');
  const [data, setData] = useState<PixelData | null>(null);

  useEffect(() => {
    setData(null);
    fetch(`/api/pixel/${x}/${y}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => {
        if (d && d.nickname) setData(d);
        else setData(null);
      })
      .catch(() => setData(null));
  }, [x, y]);

  const displayColor = data?.color ?? color;

  return (
    <div
      className="absolute bg-gray-800 rounded-lg p-3 shadow-xl border border-gray-700 z-50 min-w-[200px] pointer-events-none"
      style={{ left, top }}
    >
      <div className="flex justify-between items-start mb-2">
        <span className="text-xs text-gray-400">
          {t('coordinates', { x, y })}
        </span>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-white pointer-events-auto"
        >
          &times;
        </button>
      </div>
      {displayColor && (
        <div className="flex items-center gap-2 mb-1">
          <div
            className="w-4 h-4 rounded border border-gray-600"
            style={{ backgroundColor: displayColor }}
          />
          {data && (
            <span className="text-sm font-medium">
              {t('author', { name: data.nickname })}
            </span>
          )}
        </div>
      )}
      {data ? (
        <>
          <p className="text-xs text-gray-400">
            {t('placedAt', {
              time: new Date(data.timestamp).toLocaleString(),
            })}
          </p>
        </>
      ) : !displayColor ? (
        <p className="text-sm text-gray-400">-</p>
      ) : (
        <p className="text-xs text-gray-400">-</p>
      )}
    </div>
  );
}
