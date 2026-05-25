import { NextRequest, NextResponse } from 'next/server';
import { redis } from '@/lib/redis';
import { CANVAS_SIZE } from '@/types';

export async function GET(req: NextRequest) {
  const searchParams = req.nextUrl.searchParams;
  const chunkX = parseInt(searchParams.get('cx') || '0');
  const chunkY = parseInt(searchParams.get('cy') || '0');
  const chunkSize = 100;

  const startX = chunkX * chunkSize;
  const startY = chunkY * chunkSize;
  const pixels: Record<string, string> = {};

  // Get chunk pixels from Redis hash
  const pipeline = redis.pipeline();
  for (let x = startX; x < Math.min(startX + chunkSize, CANVAS_SIZE); x++) {
    for (let y = startY; y < Math.min(startY + chunkSize, CANVAS_SIZE); y++) {
      pipeline.hget('canvas:pixels', `${x},${y}`);
    }
  }

  const results = await pipeline.exec();

  let i = 0;
  for (let x = startX; x < Math.min(startX + chunkSize, CANVAS_SIZE); x++) {
    for (let y = startY; y < Math.min(startY + chunkSize, CANVAS_SIZE); y++) {
      const color = results?.[i]?.[1] as string | null;
      if (color) {
        pixels[`${x},${y}`] = color;
      }
      i++;
    }
  }

  return NextResponse.json({ chunkX, chunkY, chunkSize, pixels });
}
