import { v4 as uuidv4 } from 'uuid';
import { SignJWT, jwtVerify } from 'jose';
import { compare, hash } from 'bcryptjs';
import { cookies } from 'next/headers';
import { prisma } from './db';

const JWT_SECRET = new TextEncoder().encode(
  process.env.JWT_SECRET || 'pixel-canvas-jwt-secret-change-in-production'
);
const COOKIE_NAME = 'auth_token';
const COOKIE_MAX_AGE = 7 * 24 * 60 * 60; // 7 days

const nicknames = [
  '画师', '像素侠', '涂鸦客', '色彩师', '点阵王',
  '小画家', '像素狂', '涂色者', '画布师', '色块侠',
];

export function generateNickname(): string {
  const prefix = nicknames[Math.floor(Math.random() * nicknames.length)];
  const suffix = Math.floor(Math.random() * 9000) + 1000;
  return `${prefix}_${suffix}`;
}

export async function getOrCreateUser(userId?: string) {
  if (userId) {
    const existing = await prisma.user.findUnique({ where: { id: userId } });
    if (existing) return existing;
  }

  const id = userId || uuidv4();
  const nickname = generateNickname();

  return prisma.user.create({
    data: { id, nickname },
  });
}

export async function getUserFromGithub(githubId: string) {
  return prisma.user.findUnique({ where: { githubId } });
}

export async function linkGithub(userId: string, githubId: string) {
  return prisma.user.update({
    where: { id: userId },
    data: { githubId },
  });
}

export async function hashPassword(password: string): Promise<string> {
  return hash(password, 10);
}

export async function verifyPassword(
  password: string,
  hashed: string
): Promise<boolean> {
  return compare(password, hashed);
}

export async function createToken(userId: string): Promise<string> {
  return new SignJWT({ userId })
    .setProtectedHeader({ alg: 'HS256' })
    .setExpirationTime('7d')
    .sign(JWT_SECRET);
}

export async function verifyToken(
  token: string
): Promise<{ userId: string } | null> {
  try {
    const { payload } = await jwtVerify(token, JWT_SECRET);
    return { userId: payload.userId as string };
  } catch {
    return null;
  }
}

export async function setAuthCookie(userId: string) {
  const token = await createToken(userId);
  const cookieStore = await cookies();
  cookieStore.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge: COOKIE_MAX_AGE,
    path: '/',
  });
}

export async function removeAuthCookie() {
  const cookieStore = await cookies();
  cookieStore.delete(COOKIE_NAME);
}

export async function getUserFromCookie(): Promise<string | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(COOKIE_NAME)?.value;
  if (!token) return null;
  const result = await verifyToken(token);
  return result?.userId ?? null;
}

export async function findUserByEmailOrUsername(login: string) {
  return prisma.user.findFirst({
    where: {
      OR: [{ email: login }, { nickname: login }],
    },
  });
}
