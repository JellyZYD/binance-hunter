export interface Pixel {
  x: number;
  y: number;
  color: string;
  userId: string;
  nickname: string;
  timestamp: number;
}

export interface PixelPlacement {
  x: number;
  y: number;
  color: string;
}

export interface ChatMessage {
  id: string;
  userId: string;
  nickname: string;
  content: string;
  timestamp: number;
}

export interface UserStats {
  totalPixels: number;
  joinedAt: string;
}

export interface CanvasStats {
  onlineUsers: number;
  totalPixels: number;
}

export const CANVAS_SIZE = 2000;
export const MAX_POINTS = 100;
export const POINT_INTERVAL_MS = 5 * 60 * 1000;
export const HEARTBEAT_INTERVAL_MS = 30 * 1000;
export const CHAT_COOLDOWN_MS = 5 * 1000;
export const MAX_MESSAGE_LENGTH = 200;

export const COLOR_PALETTE = [
  '#FFFFFF', '#C0C0C0', '#808080', '#000000',
  '#FF0000', '#FF4500', '#FFA500', '#FFD700',
  '#FFFF00', '#ADFF2F', '#00FF00', '#008000',
  '#00FFFF', '#0000FF', '#4B0082', '#8B00FF',
  '#FF69B4', '#FF1493', '#C71585', '#8B4513',
  '#A0522D', '#D2691E', '#F4A460', '#FFDEAD',
  '#E6E6FA', '#DDA0DD', '#9370DB', '#7B68EE',
  '#4169E1', '#1E90FF', '#87CEEB', '#B0E0E6',
];
