'use client';

import { useTranslations } from 'next-intl';
import { COLOR_PALETTE } from '@/types';

interface Props {
  selectedColor: string;
  onColorSelect: (color: string) => void;
}

export default function ColorPalette({ selectedColor, onColorSelect }: Props) {
  const t = useTranslations('canvas');

  return (
    <div className="bg-gray-800 rounded-lg p-3">
      <p className="text-sm text-gray-400 mb-2">{t('selectColor')}</p>
      <div className="grid grid-cols-8 gap-1">
        {COLOR_PALETTE.map((color) => (
          <button
            key={color}
            onClick={() => onColorSelect(color)}
            className={`w-6 h-6 rounded border-2 transition-transform hover:scale-110 ${
              selectedColor === color
                ? 'border-white scale-110'
                : 'border-gray-600'
            }`}
            style={{ backgroundColor: color }}
            title={color}
          />
        ))}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="color"
          value={selectedColor}
          onChange={(e) => onColorSelect(e.target.value)}
          className="w-8 h-8 cursor-pointer"
        />
        <span className="text-xs text-gray-400">{t('customColor')}</span>
      </div>
    </div>
  );
}
