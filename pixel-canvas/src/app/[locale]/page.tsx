'use client';

import { useTranslations } from 'next-intl';
import dynamic from 'next/dynamic';
import LanguageSwitcher from '@/components/ui/LanguageSwitcher';
import AdBanner from '@/components/ui/AdBanner';

const PixelCanvas = dynamic(
  () => import('@/components/canvas/PixelCanvas'),
  { ssr: false }
);
const ChatPanel = dynamic(() => import('@/components/chat/ChatPanel'), {
  ssr: false,
});
const PointsDisplay = dynamic(
  () => import('@/components/points/PointsDisplay'),
  { ssr: false }
);

export default function HomePage() {
  const t = useTranslations();

  return (
    <div className="h-screen flex flex-col">
      {/* Header */}
      <header className="bg-gray-800 border-b border-gray-700 px-4 py-2 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">{t('common.title')}</h1>
          <p className="text-xs text-gray-400">{t('common.subtitle')}</p>
        </div>
        <div className="flex items-center gap-4">
          <PointsDisplay />
          <LanguageSwitcher />
        </div>
      </header>

      {/* Top Ad */}
      <AdBanner
        slot="top-banner"
        format="horizontal"
        className="flex justify-center py-2 bg-gray-900"
      />

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Canvas */}
        <PixelCanvas />

        {/* Sidebar */}
        <div className="flex flex-col">
          <ChatPanel />
          {/* Side Ad */}
          <AdBanner
            slot="side-rectangle"
            format="rectangle"
            className="p-2 bg-gray-800"
          />
        </div>
      </div>

      {/* Footer */}
      <footer className="bg-gray-800 border-t border-gray-700 px-4 py-2 text-center text-xs text-gray-500">
        Pixel Canvas &copy; {new Date().getFullYear()}
      </footer>
    </div>
  );
}
