import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Binance Pump-Dump Hunter',
  description: 'Binance futures pump-dump short signal dashboard',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
