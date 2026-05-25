'use client';

import { useState, useRef, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { useChat } from '@/hooks/useChat';
import ChatMessage from './ChatMessage';
import { MAX_MESSAGE_LENGTH } from '@/types';

export default function ChatPanel() {
  const t = useTranslations('chat');
  const { messages, sendMessage, cooldown, onlineCount } = useChat();
  const [input, setInput] = useState('');
  const [isOpen, setIsOpen] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = () => {
    if (input.trim() && cooldown === 0) {
      sendMessage(input.trim());
      setInput('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div
      className={`flex flex-col bg-gray-800 transition-all ${isOpen ? 'w-80' : 'w-12'}`}
    >
      {/* Toggle Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="h-10 flex items-center justify-center bg-gray-700 hover:bg-gray-600"
      >
        {isOpen ? '>' : '<'}
      </button>

      {isOpen && (
        <>
          {/* Header */}
          <div className="p-3 border-b border-gray-700">
            <h3 className="font-bold">{t('title')}</h3>
            <p className="text-xs text-gray-400">
              {t('onlineUsers', { count: onlineCount })}
            </p>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-2 space-y-1 min-h-0">
            {messages.length === 0 ? (
              <p className="text-sm text-gray-500 text-center py-4">
                {t('noMessages')}
              </p>
            ) : (
              messages.map((msg) => <ChatMessage key={msg.id} message={msg} />)
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="p-3 border-t border-gray-700">
            <div className="flex gap-2">
              <input
                type="text"
                value={input}
                onChange={(e) =>
                  setInput(e.target.value.slice(0, MAX_MESSAGE_LENGTH))
                }
                onKeyDown={handleKeyDown}
                placeholder={t('placeholder')}
                disabled={cooldown > 0}
                className="flex-1 bg-gray-700 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
              />
              <button
                onClick={handleSend}
                disabled={cooldown > 0 || !input.trim()}
                className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 px-4 py-2 rounded text-sm font-medium transition-colors"
              >
                {cooldown > 0 ? `${cooldown}s` : t('send')}
              </button>
            </div>
            <p className="text-xs text-gray-500 mt-1">
              {input.length}/{MAX_MESSAGE_LENGTH}
            </p>
          </div>
        </>
      )}
    </div>
  );
}
