import { defineRouting } from 'next-intl/routing';

export const locales = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'pt', 'ru', 'ar', 'hi'] as const;
export const defaultLocale = 'zh-CN' as const;
export type Locale = (typeof locales)[number];

export const routing = defineRouting({
  locales,
  defaultLocale
});
