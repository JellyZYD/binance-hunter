import createMiddleware from 'next-intl/middleware';
import { routing } from './src/i18n/routing';

export default createMiddleware(routing);

export const config = {
  matcher: ['/', '/(zh-CN|zh-TW|en|ja|ko|fr|de|es|pt|ru|ar|hi)', '/(zh-CN|zh-TW|en|ja|ko|fr|de|es|pt|ru|ar|hi)/:path*']
};
