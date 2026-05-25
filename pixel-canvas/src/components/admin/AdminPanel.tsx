'use client';

import { useState, useEffect, useCallback } from 'react';
import { useTranslations } from 'next-intl';

interface Stats {
  onlineUsers: number;
  totalPixels: number;
}

interface Config {
  initialPoints: number;
  maxPoints: number;
  pointInterval: number;
}

interface ChatMessage {
  id: string;
  userId: string;
  nickname: string;
  content: string;
  timestamp: number;
}

interface UserInfo {
  id: string;
  nickname: string;
  email: string | null;
  totalPixels: number;
  createdAt: string;
}

export default function AdminPanel() {
  const t = useTranslations('admin');
  const [password, setPassword] = useState('');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [stats, setStats] = useState<Stats>({ onlineUsers: 0, totalPixels: 0 });
  const [config, setConfig] = useState<Config>({
    initialPoints: 100,
    maxPoints: 100,
    pointInterval: 300000,
  });
  const [rollbackCount, setRollbackCount] = useState(100);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatTotal, setChatTotal] = useState(0);
  const [chatPage, setChatPage] = useState(1);
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [usersTotal, setUsersTotal] = useState(0);
  const [usersPage, setUsersPage] = useState(1);

  const headers = { 'x-admin-password': password, 'Content-Type': 'application/json' };

  const fetchStats = useCallback(async () => {
    const res = await fetch('/api/stats');
    const data = await res.json();
    setStats(data);
  }, []);

  const fetchConfig = useCallback(async () => {
    const res = await fetch('/api/admin/config', { headers });
    if (res.ok) {
      const data = await res.json();
      setConfig(data);
    }
  }, [password]);

  const fetchChat = useCallback(async (page: number) => {
    const res = await fetch(`/api/admin/chat?page=${page}&limit=20`, { headers });
    if (res.ok) {
      const data = await res.json();
      setChatMessages(data.messages);
      setChatTotal(data.total);
    }
  }, [password]);

  const fetchUsers = useCallback(async (page: number) => {
    const res = await fetch(`/api/admin/users?page=${page}&limit=20`, { headers });
    if (res.ok) {
      const data = await res.json();
      setUsers(data.users);
      setUsersTotal(data.total);
    }
  }, [password]);

  useEffect(() => {
    if (!isAuthenticated) return;

    fetchStats();
    fetchConfig();
    fetchChat(1);
    fetchUsers(1);

    const interval = setInterval(fetchStats, 5000);
    return () => clearInterval(interval);
  }, [isAuthenticated, fetchStats, fetchConfig, fetchChat, fetchUsers]);

  const handleLogin = () => {
    if (password === process.env.NEXT_PUBLIC_ADMIN_PASSWORD) {
      setIsAuthenticated(true);
    }
  };

  const handleSaveConfig = async () => {
    await fetch('/api/admin/config', {
      method: 'POST',
      headers,
      body: JSON.stringify({ password, ...config }),
    });
    alert('Config saved!');
  };

  const handleReset = async () => {
    if (!confirm(t('resetConfirm'))) return;
    await fetch('/api/admin/reset', {
      method: 'POST',
      headers,
      body: JSON.stringify({ password }),
    });
    alert('Canvas reset!');
    fetchStats();
  };

  const handleRollback = async () => {
    await fetch('/api/admin/rollback', {
      method: 'POST',
      headers,
      body: JSON.stringify({ password, count: rollbackCount }),
    });
    alert(`Rolled back ${rollbackCount} pixels!`);
  };

  const handleDeleteMessage = async (messageId: string) => {
    await fetch('/api/admin/chat', {
      method: 'DELETE',
      headers,
      body: JSON.stringify({ password, messageId }),
    });
    fetchChat(chatPage);
  };

  const handleClearChat = async () => {
    if (!confirm('Clear all chat messages?')) return;
    await fetch('/api/admin/chat', {
      method: 'DELETE',
      headers,
      body: JSON.stringify({ password, clearAll: true }),
    });
    fetchChat(1);
  };

  const handleDeleteUser = async (userId: string) => {
    if (!confirm('Delete this user and all their pixel history?')) return;
    await fetch('/api/admin/users', {
      method: 'DELETE',
      headers,
      body: JSON.stringify({ password, userId }),
    });
    fetchUsers(usersPage);
    fetchStats();
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
            className="w-full bg-gray-700 rounded px-4 py-3 mb-4 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
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
          <p className="text-4xl font-bold text-blue-400">{stats.onlineUsers}</p>
        </div>
        <div className="bg-gray-800 p-6 rounded-lg">
          <p className="text-sm text-gray-400">{t('totalPixels')}</p>
          <p className="text-4xl font-bold text-green-400">{stats.totalPixels}</p>
        </div>
      </div>

      {/* Points Config */}
      <div className="bg-gray-800 p-6 rounded-lg mb-8">
        <h3 className="font-bold mb-4 text-lg">{t('pointsConfig')}</h3>
        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="text-sm text-gray-400 block mb-1">{t('initialPoints')}</label>
            <input
              type="number"
              value={config.initialPoints}
              onChange={(e) => setConfig({ ...config, initialPoints: parseInt(e.target.value) || 0 })}
              className="w-full bg-gray-700 rounded px-3 py-2 text-white focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="text-sm text-gray-400 block mb-1">{t('maxPoints')}</label>
            <input
              type="number"
              value={config.maxPoints}
              onChange={(e) => setConfig({ ...config, maxPoints: parseInt(e.target.value) || 1 })}
              className="w-full bg-gray-700 rounded px-3 py-2 text-white focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="text-sm text-gray-400 block mb-1">{t('pointInterval')}</label>
            <input
              type="number"
              value={config.pointInterval}
              onChange={(e) => setConfig({ ...config, pointInterval: parseInt(e.target.value) || 60000 })}
              className="w-full bg-gray-700 rounded px-3 py-2 text-white focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <p className="text-xs text-gray-500 mt-1">{Math.round(config.pointInterval / 60000)} min</p>
          </div>
        </div>
        <button
          onClick={handleSaveConfig}
          className="mt-4 bg-green-600 hover:bg-green-700 px-6 py-2 rounded transition-colors"
        >
          {t('saveConfig')}
        </button>
      </div>

      {/* Canvas Actions */}
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
            className="w-full bg-gray-700 rounded px-3 py-2 mb-3 text-white focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <button
            onClick={handleRollback}
            className="w-full bg-yellow-600 hover:bg-yellow-700 py-2 rounded transition-colors"
          >
            {t('rollback')}
          </button>
        </div>
      </div>

      {/* Chat Management */}
      <div className="bg-gray-800 p-6 rounded-lg">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-lg">{t('chatManagement')}</h3>
          <div className="flex gap-2">
            <span className="text-sm text-gray-400">{t('totalMessages')}: {chatTotal}</span>
            <button
              onClick={handleClearChat}
              className="bg-red-600 hover:bg-red-700 px-4 py-1 rounded text-sm transition-colors"
            >
              {t('clearChat')}
            </button>
          </div>
        </div>

        <div className="space-y-2 max-h-96 overflow-y-auto">
          {chatMessages.length === 0 ? (
            <p className="text-gray-500 text-center py-4">No messages</p>
          ) : (
            chatMessages.map((msg) => (
              <div key={msg.id} className="flex items-start gap-3 bg-gray-700 p-3 rounded">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-blue-400">{msg.nickname}</span>
                    <span className="text-xs text-gray-500">
                      {new Date(msg.timestamp).toLocaleString()}
                    </span>
                  </div>
                  <p className="text-sm mt-1">{msg.content}</p>
                </div>
                <button
                  onClick={() => handleDeleteMessage(msg.id)}
                  className="text-red-400 hover:text-red-300 text-sm"
                >
                  {t('delete')}
                </button>
              </div>
            ))
          )}
        </div>

        {/* Pagination */}
        {chatTotal > 20 && (
          <div className="flex justify-center gap-2 mt-4">
            <button
              onClick={() => { setChatPage(chatPage - 1); fetchChat(chatPage - 1); }}
              disabled={chatPage <= 1}
              className="px-3 py-1 bg-gray-700 rounded disabled:opacity-50 hover:bg-gray-600"
            >
              Prev
            </button>
            <span className="px-3 py-1 text-sm text-gray-400">
              {chatPage} / {Math.ceil(chatTotal / 20)}
            </span>
            <button
              onClick={() => { setChatPage(chatPage + 1); fetchChat(chatPage + 1); }}
              disabled={chatPage >= Math.ceil(chatTotal / 20)}
              className="px-3 py-1 bg-gray-700 rounded disabled:opacity-50 hover:bg-gray-600"
            >
              Next
            </button>
          </div>
        )}
      </div>

      {/* User Management */}
      <div className="bg-gray-800 p-6 rounded-lg mt-8">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-lg">用户管理</h3>
          <span className="text-sm text-gray-400">总用户: {usersTotal}</span>
        </div>

        <div className="space-y-2 max-h-96 overflow-y-auto">
          {users.length === 0 ? (
            <p className="text-gray-500 text-center py-4">No users</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 text-left">
                  <th className="pb-2">用户名</th>
                  <th className="pb-2">邮箱</th>
                  <th className="pb-2">像素数</th>
                  <th className="pb-2">注册时间</th>
                  <th className="pb-2">操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id} className="border-t border-gray-700">
                    <td className="py-2 font-medium text-blue-400">{u.nickname}</td>
                    <td className="py-2 text-gray-400">{u.email || '-'}</td>
                    <td className="py-2">{u.totalPixels}</td>
                    <td className="py-2 text-gray-500">{new Date(u.createdAt).toLocaleDateString()}</td>
                    <td className="py-2">
                      <button
                        onClick={() => handleDeleteUser(u.id)}
                        className="text-red-400 hover:text-red-300 text-xs"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Pagination */}
        {usersTotal > 20 && (
          <div className="flex justify-center gap-2 mt-4">
            <button
              onClick={() => { setUsersPage(usersPage - 1); fetchUsers(usersPage - 1); }}
              disabled={usersPage <= 1}
              className="px-3 py-1 bg-gray-700 rounded disabled:opacity-50 hover:bg-gray-600"
            >
              Prev
            </button>
            <span className="px-3 py-1 text-sm text-gray-400">
              {usersPage} / {Math.ceil(usersTotal / 20)}
            </span>
            <button
              onClick={() => { setUsersPage(usersPage + 1); fetchUsers(usersPage + 1); }}
              disabled={usersPage >= Math.ceil(usersTotal / 20)}
              className="px-3 py-1 bg-gray-700 rounded disabled:opacity-50 hover:bg-gray-600"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
