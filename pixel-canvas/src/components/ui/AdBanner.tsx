'use client';

import { useEffect, useRef } from 'react';

interface Props {
  slot: string;
  format?: 'horizontal' | 'rectangle' | 'small';
  className?: string;
}

export default function AdBanner({
  slot,
  format = 'horizontal',
  className = '',
}: Props) {
  const adRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Load Google AdSense
    try {
      ((window as any).adsbygoogle = (window as any).adsbygoogle || []).push(
        {}
      );
    } catch (err) {
      console.error('AdSense error:', err);
    }
  }, []);

  const dimensions = {
    horizontal: { width: 728, height: 90 },
    rectangle: { width: 300, height: 250 },
    small: { width: 320, height: 50 },
  };

  const { width, height } = dimensions[format];

  return (
    <div ref={adRef} className={`ad-container ${className}`}>
      <ins
        className="adsbygoogle"
        style={{ display: 'inline-block', width, height }}
        data-ad-client={process.env.NEXT_PUBLIC_ADSENSE_CLIENT}
        data-ad-slot={slot}
        data-ad-format="auto"
        data-full-width-responsive="true"
      />
    </div>
  );
}
