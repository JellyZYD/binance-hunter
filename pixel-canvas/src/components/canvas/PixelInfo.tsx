'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useState } from 'react';

interface Props {
  x: number;
  y: number;
  onClose: () => void;
}

interface PixelData {
  nickname: string;
  timestamp: number;
  color: string;
}

export default function PixelInfo({ x, y, onClose }: Props) {
  const t = useTranslations('pixelInfo');
  const [data, setData] = useState<PixelData | null>(null);

  useEffect(() => {
    fetch(`/api/pixel/${x}/${y}`)
      .then((res) => res.json())
      .then(setData)
      .catch(() => setData(null));
  }, [x, y]);

  return (
    <div className="absolute bg-gray-800 rounded-lg p-3 shadow-xl border border-gray-700 z-50 min-w-[200px]">
      <div className="flex justify-between items-start mb-2">
        <span className="text-xs text-gray-400">
          {t('coordinates', { x, y })}
        </span>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-white"
        >
          &times;
        </button>
      </div>
      {data ? (
        <>
          <div className="flex items-center gap-2 mb-1">
            <div
              className="w-4 h-4 rounded"
              style={{ backgroundColor: data.color }}
            />
            <span className="text-sm font-medium">
              {t('author', { name: data.nickname })}
            </span>
          </div>
          <p className="text-xs text-gray-400">
            {t('placedAt', {
              time: new Date(data.timestamp).toLocaleString(),
            })}
          </p>
        </>
      ) : (
        <p className="text-sm text-gray-400">-</p>
      )}
    </div>
  );
}
