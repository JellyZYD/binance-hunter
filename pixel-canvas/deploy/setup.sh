#!/bin/bash
# Pixel Canvas 一键部署脚本 (Ubuntu)
# 用法: sudo bash deploy/setup.sh

set -e

echo "=== Pixel Canvas 部署开始 ==="

# 1. 安装系统依赖
echo "[1/7] 安装系统依赖..."
apt update && apt upgrade -y
apt install -y curl git nginx certbot python3-certbot-nginx

# 2. 安装 Node.js 20
echo "[2/7] 安装 Node.js 20..."
if ! command -v node &> /dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt install -y nodejs
fi
echo "Node.js $(node -v), npm $(npm -v)"

# 3. 安装 PostgreSQL
echo "[3/7] 安装 PostgreSQL..."
if ! command -v psql &> /dev/null; then
  apt install -y postgresql postgresql-contrib
  systemctl enable postgresql
  systemctl start postgresql
fi

# 创建数据库和用户
sudo -u postgres psql -c "CREATE USER pixelcanvas WITH PASSWORD 'pixelcanvas123';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE pixel_canvas OWNER pixelcanvas;" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE pixel_canvas TO pixelcanvas;" 2>/dev/null || true

# 4. 安装 Redis
echo "[4/7] 安装 Redis..."
if ! command -v redis-server &> /dev/null; then
  apt install -y redis-server
  systemctl enable redis-server
  systemctl start redis-server
fi

# 5. 安装 PM2
echo "[5/7] 安装 PM2..."
npm install -g pm2

# 6. 部署应用
echo "[6/7] 部署应用..."
APP_DIR="/opt/pixel-canvas"
mkdir -p $APP_DIR

# 复制项目文件（排除 node_modules 和 .next）
rsync -av --exclude='node_modules' --exclude='.next' --exclude='.git' \
  ./ $APP_DIR/

cd $APP_DIR

# 创建 .env 文件
if [ ! -f .env ]; then
  cat > .env << 'EOF'
DATABASE_URL="postgresql://pixelcanvas:pixelcanvas123@localhost:5432/pixel_canvas"
REDIS_URL="redis://localhost:6379"
ADMIN_PASSWORD="admin123"
NEXT_PUBLIC_ADMIN_PASSWORD="admin123"
SOCKET_PORT=3001
NEXT_PUBLIC_SOCKET_URL="http://YOUR_DOMAIN:3001"
NEXT_PUBLIC_APP_URL="http://YOUR_DOMAIN"
JWT_SECRET="change-this-to-a-random-string-in-production"
EOF
  echo "请编辑 $APP_DIR/.env 修改域名和密码！"
fi

# 安装依赖并构建
npm install --production=false
npx prisma generate
npx prisma db push --accept-data-loss
npm run build

# 7. 配置 PM2 和 Nginx
echo "[7/7] 配置 PM2 和 Nginx..."

# PM2 ecosystem 文件
cat > ecosystem.config.js << 'EOF'
module.exports = {
  apps: [
    {
      name: 'pixel-canvas',
      script: 'node_modules/.bin/next',
      args: 'start',
      cwd: '/opt/pixel-canvas',
      env: {
        NODE_ENV: 'production',
        PORT: 3000,
      },
      max_memory_restart: '300M',
    },
    {
      name: 'pixel-socket',
      script: 'node_modules/.bin/ts-node',
      args: '--project tsconfig.server.json server/socket.ts',
      cwd: '/opt/pixel-canvas',
      env: {
        NODE_ENV: 'production',
      },
      max_memory_restart: '100M',
    },
  ],
};
EOF

pm2 start ecosystem.config.js
pm2 save
pm2 startup

# Nginx 配置
cat > /etc/nginx/sites-available/pixel-canvas << 'EOF'
server {
    listen 80;
    server_name YOUR_DOMAIN;

    # Next.js 应用
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # Socket.io
    location /socket.io/ {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

ln -sf /etc/nginx/sites-available/pixel-canvas /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ""
echo "=== 部署完成！==="
echo "1. 编辑 .env 文件: nano $APP_DIR/.env"
echo "2. 修改 YOUR_DOMAIN 为你的域名或 IP"
echo "3. 重启服务: pm2 restart all"
echo "4. 如需 HTTPS: certbot --nginx -d YOUR_DOMAIN"
echo ""
