import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: '合约主力动向监控',
  description: 'Binance futures lifecycle signal dashboard',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
