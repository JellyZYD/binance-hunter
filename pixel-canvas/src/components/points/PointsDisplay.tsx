'use client';

import { useTranslations } from 'next-intl';
import { usePoints } from '@/hooks/usePoints';

export default function PointsDisplay() {
  const t = useTranslations('points');
  const { points, nextPointIn, maxPoints, pointInterval } = usePoints();

  const formatTime = (ms: number) => {
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return `${minutes}:${seconds.toString().padStart(2, '0')}`;
  };

  return (
    <div className="bg-gray-800 rounded-lg p-4">
      <h3 className="text-sm text-gray-400 mb-2">{t('title')}</h3>
      <div className="flex items-baseline gap-2">
        <span className="text-3xl font-bold text-blue-400">{points}</span>
        <span className="text-sm text-gray-500">/ {maxPoints}</span>
      </div>

      {points < maxPoints && (
        <div className="mt-2">
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>{t('nextPoint', { time: formatTime(nextPointIn) })}</span>
          </div>
          <div className="w-full bg-gray-700 rounded-full h-2">
            <div
              className="bg-blue-600 h-2 rounded-full transition-all"
              style={{
                width: `${((pointInterval - nextPointIn) / pointInterval) * 100}%`,
              }}
            />
          </div>
        </div>
      )}

      {points >= maxPoints && (
        <p className="text-xs text-green-400 mt-1">{t('maxReached')}</p>
      )}
    </div>
  );
}
