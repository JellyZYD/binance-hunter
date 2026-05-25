'use client';

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { useAuth } from '@/hooks/useAuth';
import AuthModal from './AuthModal';

export default function UserMenu() {
  const t = useTranslations('auth');
  const { user, loading, register, login, logout } = useAuth();
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<'login' | 'register'>('login');
  const [menuOpen, setMenuOpen] = useState(false);

  if (loading) {
    return (
      <div className="w-8 h-8 rounded-full bg-gray-700 animate-pulse" />
    );
  }

  if (!user) {
    return (
      <>
        <button
          onClick={() => {
            setModalMode('login');
            setModalOpen(true);
          }}
          className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium transition-colors"
        >
          {t('login')}
        </button>
        <AuthModal
          isOpen={modalOpen}
          onClose={() => setModalOpen(false)}
          mode={modalMode}
          onSwitchMode={setModalMode}
          onRegister={register}
          onLogin={login}
        />
      </>
    );
  }

  const initial = user.nickname.charAt(0).toUpperCase();

  return (
    <div className="relative">
      <button
        onClick={() => setMenuOpen(!menuOpen)}
        className="flex items-center gap-2 hover:bg-gray-700 rounded-lg px-2 py-1 transition-colors"
      >
        <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-sm font-bold">
          {initial}
        </div>
        <span className="text-sm font-medium">{user.nickname}</span>
      </button>

      {menuOpen && (
        <>
          <div
            className="fixed inset-0 z-40"
            onClick={() => setMenuOpen(false)}
          />
          <div className="absolute right-0 mt-2 w-48 bg-gray-800 rounded-xl shadow-2xl border border-gray-700 z-50 overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-700">
              <p className="text-sm font-medium">{user.nickname}</p>
              <p className="text-xs text-gray-400">ID: {user.id.slice(0, 8)}...</p>
            </div>
            <button
              onClick={() => {
                setMenuOpen(false);
                logout();
              }}
              className="w-full text-left px-4 py-2.5 text-sm text-red-400 hover:bg-gray-700 transition-colors"
            >
              {t('logout')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
