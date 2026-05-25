'use client';

import { useState, useEffect } from 'react';
import { useTranslations } from 'next-intl';

interface Stats {
  onlineUsers: number;
  totalPixels: number;
}

export default function AdminPanel() {
  const t = useTranslations('admin');
  const [password, setPassword] = useState('');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [stats, setStats] = useState<Stats>({ onlineUsers: 0, totalPixels: 0 });
  const [rollbackCount, setRollbackCount] = useState(100);
  const [adCode, setAdCode] = useState('');

  useEffect(() => {
    if (!isAuthenticated) return;

    const fetchStats = async () => {
      const res = await fetch('/api/stats');
      const data = await res.json();
      setStats(data);
    };

    fetchStats();
    const interval = setInterval(fetchStats, 5000);
    return () => clearInterval(interval);
  }, [isAuthenticated]);

  const handleLogin = () => {
    if (password === process.env.NEXT_PUBLIC_ADMIN_PASSWORD) {
      setIsAuthenticated(true);
    }
  };

  const handleReset = async () => {
    if (!confirm(t('resetConfirm'))) return;

    await fetch('/api/admin/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });

    alert('Canvas reset!');
  };

  const handleRollback = async () => {
    await fetch('/api/admin/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, count: rollbackCount }),
    });

    alert(`Rolled back ${rollbackCount} pixels!`);
  };

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-900">
        <div className="bg-gray-800 p-8 rounded-lg w-96">
          <h2 className="text-2xl font-bold mb-6">{t('title')}</h2>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t('password')}
            className="w-full bg-gray-700 rounded px-4 py-3 mb-4 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <button
            onClick={handleLogin}
            className="w-full bg-blue-600 hover:bg-blue-700 py-3 rounded font-bold transition-colors"
          >
            {t('login')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-900 p-8">
      <h1 className="text-3xl font-bold mb-8">{t('title')}</h1>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-4 mb-8">
        <div className="bg-gray-800 p-6 rounded-lg">
          <p className="text-sm text-gray-400">{t('onlineUsers')}</p>
          <p className="text-4xl font-bold text-blue-400">
            {stats.onlineUsers}
          </p>
        </div>
        <div className="bg-gray-800 p-6 rounded-lg">
          <p className="text-sm text-gray-400">{t('totalPixels')}</p>
          <p className="text-4xl font-bold text-green-400">
            {stats.totalPixels}
          </p>
        </div>
      </div>

      {/* Actions */}
      <div className="grid grid-cols-2 gap-4 mb-8">
        <div className="bg-gray-800 p-6 rounded-lg">
          <h3 className="font-bold mb-4">{t('reset')}</h3>
          <button
            onClick={handleReset}
            className="w-full bg-red-600 hover:bg-red-700 py-2 rounded transition-colors"
          >
            {t('reset')}
          </button>
        </div>

        <div className="bg-gray-800 p-6 rounded-lg">
          <h3 className="font-bold mb-4">{t('rollback')}</h3>
          <input
            type="number"
            value={rollbackCount}
            onChange={(e) => setRollbackCount(parseInt(e.target.value))}
            className="w-full bg-gray-700 rounded px-3 py-2 mb-3 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <button
            onClick={handleRollback}
            className="w-full bg-yellow-600 hover:bg-yellow-700 py-2 rounded transition-colors"
          >
            {t('rollback')}
          </button>
        </div>
      </div>

      {/* Ad Code */}
      <div className="bg-gray-800 p-6 rounded-lg">
        <h3 className="font-bold mb-4">{t('adCode')}</h3>
        <textarea
          value={adCode}
          onChange={(e) => setAdCode(e.target.value)}
          className="w-full bg-gray-700 rounded px-3 py-2 h-32 font-mono text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          placeholder="<script>...</script>"
        />
        <button
          onClick={() => {
            /* Save ad code */
          }}
          className="mt-3 bg-green-600 hover:bg-green-700 px-6 py-2 rounded transition-colors"
        >
          {t('saveAdCode')}
        </button>
      </div>
    </div>
  );
}
