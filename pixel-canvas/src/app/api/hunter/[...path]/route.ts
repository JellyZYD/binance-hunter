import { NextRequest, NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

const DEFAULT_BASE_URL = 'http://127.0.0.1:8787';

export async function GET(
  req: NextRequest,
  context: { params: Promise<{ path?: string[] }> }
) {
  const { path = [] } = await context.params;
  const baseUrl = (process.env.HUNTER_API_BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, '');
  const search = req.nextUrl.search || '';
  const target = `${baseUrl}/api/${path.join('/')}${search}`;

  try {
    const upstream = await fetch(target, { cache: 'no-store' });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        'Content-Type': upstream.headers.get('Content-Type') || 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
      },
    });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error), target },
      { status: 502 }
    );
  }
}
