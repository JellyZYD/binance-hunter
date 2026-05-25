'use client';

import { useLocale, useTranslations } from 'next-intl';
import { useRouter, usePathname } from 'next/navigation';
import { useState, useRef, useEffect } from 'react';
import { locales, type Locale } from '@/i18n/routing';

const localeFlags: Record<Locale, string> = {
  'zh-CN': '🇨🇳',
  'zh-TW': '🇹🇼',
  en: '🇺🇸',
  ja: '🇯🇵',
  ko: '🇰🇷',
  fr: '🇫🇷',
  de: '🇩🇪',
  es: '🇪🇸',
  pt: '🇧🇷',
  ru: '🇷🇺',
  ar: '🇸🇦',
  hi: '🇮🇳',
};

export default function LanguageSwitcher() {
  const locale = useLocale() as Locale;
  const t = useTranslations('language');
  const router = useRouter();
  const pathname = usePathname();
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  function switchLocale(newLocale: Locale) {
    const path = pathname.replace(`/${locale}`, `/${newLocale}`);
    router.push(path);
    setIsOpen(false);
    localStorage.setItem('locale', newLocale);
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-2 bg-gray-800 rounded-lg hover:bg-gray-700 transition-colors"
      >
        <span>{localeFlags[locale]}</span>
        <span className="text-sm">{t(locale)}</span>
        <svg
          className="w-4 h-4"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M19 9l-7 7-7-7"
          />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute right-0 top-full mt-1 bg-gray-800 rounded-lg shadow-xl border border-gray-700 overflow-hidden z-50 min-w-[160px]">
          {locales.map((l) => (
            <button
              key={l}
              onClick={() => switchLocale(l)}
              className={`w-full flex items-center gap-2 px-4 py-2 text-sm hover:bg-gray-700 transition-colors ${
                l === locale ? 'bg-gray-700 text-blue-400' : ''
              }`}
            >
              <span>{localeFlags[l]}</span>
              <span>{t(l)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
