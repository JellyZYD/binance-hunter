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
    <div className="py-1 px-2 hover:bg-gray-800 rounded">
      <span className="text-xs text-gray-500 mr-2">{time}</span>
      <span className="text-sm font-medium text-blue-400">
        {message.nickname}
      </span>
      <span className="text-sm text-gray-300">: {message.content}</span>
    </div>
  );
}
