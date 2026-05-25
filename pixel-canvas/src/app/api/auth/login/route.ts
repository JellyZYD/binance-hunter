import { NextRequest, NextResponse } from 'next/server';
import { findUserByEmailOrUsername, verifyPassword, setAuthCookie } from '@/lib/auth';

export async function POST(req: NextRequest) {
  const { login, password } = await req.json();

  if (!login || !password) {
    return NextResponse.json(
      { error: 'Login and password required' },
      { status: 400 }
    );
  }

  const user = await findUserByEmailOrUsername(login);
  if (!user || !user.password) {
    return NextResponse.json(
      { error: 'Invalid credentials' },
      { status: 401 }
    );
  }

  const valid = await verifyPassword(password, user.password);
  if (!valid) {
    return NextResponse.json(
      { error: 'Invalid credentials' },
      { status: 401 }
    );
  }

  await setAuthCookie(user.id);

  return NextResponse.json({
    user: { id: user.id, nickname: user.nickname },
  });
}
