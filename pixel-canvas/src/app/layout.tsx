import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Pixel Canvas - 协作像素画布',
  description: '合力绘制巨型像素画，每人每5分钟一个像素点',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
