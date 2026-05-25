import { NextRequest, NextResponse } from 'next/server';
import { prisma } from '@/lib/db';
import { generateNickname } from '@/lib/auth';

export async function GET(req: NextRequest) {
  const code = req.nextUrl.searchParams.get('code');

  if (!code) {
    return NextResponse.redirect(new URL('/', req.url));
  }

  // Exchange code for access token
  const tokenRes = await fetch('https://github.com/login/oauth/access_token', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({
      client_id: process.env.GITHUB_CLIENT_ID,
      client_secret: process.env.GITHUB_CLIENT_SECRET,
      code,
    }),
  });

  const { access_token } = await tokenRes.json();

  // Get user info
  const userRes = await fetch('https://api.github.com/user', {
    headers: { Authorization: `Bearer ${access_token}` },
  });

  const githubUser = await userRes.json();

  // Find or create user
  let user = await prisma.user.findUnique({
    where: { githubId: String(githubUser.id) },
  });

  if (!user) {
    user = await prisma.user.create({
      data: {
        nickname: githubUser.login || generateNickname(),
        githubId: String(githubUser.id),
      },
    });
  }

  // Set cookie and redirect
  const response = NextResponse.redirect(new URL('/', req.url));
  response.cookies.set('userId', user.id, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge: 60 * 60 * 24 * 365,
  });

  return response;
}
