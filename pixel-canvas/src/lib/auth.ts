import { v4 as uuidv4 } from 'uuid';
import { prisma } from './db';

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
