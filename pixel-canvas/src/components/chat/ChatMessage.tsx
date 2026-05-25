'use client';

import { ChatMessage as MessageType } from '@/types';

interface Props {
  message: MessageType;
}

export default function ChatMessage({ message }: Props) {
  const time = new Date(message.timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });

  return (
    <div className="py-1.5 px-3 bg-gray-700/50 rounded-xl">
      <div className="flex items-baseline gap-2">
        <span className="text-xs text-blue-400 font-medium">
          {message.nickname}
        </span>
        <span className="text-xs text-gray-500">{time}</span>
      </div>
      <p className="text-sm text-gray-200 mt-0.5 break-words">{message.content}</p>
    </div>
  );
}
