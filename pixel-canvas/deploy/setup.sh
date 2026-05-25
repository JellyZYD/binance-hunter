#!/bin/bash
# Pixel Canvas 一键部署脚本 (Ubuntu)
# 用法: sudo bash deploy/setup.sh
# 域名: pixia.cc

set -e

DOMAIN="pixia.cc"
ADMIN_PASSWORD="123456"
DB_PASSWORD="pixelcanvas_$(openssl rand -hex 8)"
JWT_SECRET="$(openssl rand -hex 32)"
APP_DIR="/opt/pixel-canvas"

echo "============================================"
echo "  Pixel Canvas 一键部署"
echo "  域名: $DOMAIN"
echo "============================================"

# 1. 安装系统依赖
echo ""
echo "[1/8] 安装系统依赖..."
apt update && apt upgrade -y
apt install -y curl git nginx openssl

# 2. 安装 Node.js 20
echo ""
echo "[2/8] 安装 Node.js 20..."
if ! command -v node &> /dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt install -y nodejs
fi
echo "Node.js $(node -v), npm $(npm -v)"

# 3. 安装 PostgreSQL
echo ""
echo "[3/8] 安装 PostgreSQL..."
if ! command -v psql &> /dev/null; then
  apt install -y postgresql postgresql-contrib
  systemctl enable postgresql
  systemctl start postgresql
fi

# 创建数据库和用户
sudo -u postgres psql -c "DROP DATABASE IF EXISTS pixel_canvas;" 2>/dev/null || true
sudo -u postgres psql -c "DROP USER IF EXISTS pixelcanvas;" 2>/dev/null || true
sudo -u postgres psql -c "CREATE USER pixelcanvas WITH PASSWORD '$DB_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE pixel_canvas OWNER pixelcanvas;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE pixel_canvas TO pixelcanvas;"
echo "数据库创建完成"

# 4. 安装 Redis
echo ""
echo "[4/8] 安装 Redis..."
if ! command -v redis-server &> /dev/null; then
  apt install -y redis-server
  systemctl enable redis-server
  systemctl start redis-server
fi
echo "Redis 已启动"

# 5. 克隆代码
echo ""
echo "[5/8] 克隆代码..."
if [ -d "$APP_DIR" ]; then
  cd $APP_DIR && git pull origin main
else
  git clone https://github.com/JellyZYD/pixel-canvas.git $APP_DIR
  cd $APP_DIR
fi

# 6. 生成配置文件
echo ""
echo "[6/8] 生成配置文件..."
cat > $APP_DIR/.env << EOF
DATABASE_URL="postgresql://pixelcanvas:${DB_PASSWORD}@localhost:5432/pixel_canvas"
REDIS_URL="redis://localhost:6379"
ADMIN_PASSWORD="${ADMIN_PASSWORD}"
NEXT_PUBLIC_ADMIN_PASSWORD="${ADMIN_PASSWORD}"
JWT_SECRET="${JWT_SECRET}"
SOCKET_PORT=3001
NEXT_PUBLIC_SOCKET_URL="https://${DOMAIN}"
NEXT_PUBLIC_APP_URL="https://${DOMAIN}"
EOF

# 7. 安装依赖并构建
echo ""
echo "[7/8] 安装依赖并构建..."
cd $APP_DIR
npm install
npx prisma generate
npx prisma db push --accept-data-loss
npm run build

# 8. 配置 PM2 和 Nginx
echo ""
echo "[8/8] 配置 PM2 和 Nginx..."

# 安装 PM2
npm install -g pm2

# PM2 配置
cat > $APP_DIR/ecosystem.config.js << 'PMEOF'
module.exports = {
  apps: [
    {
      name: 'pixel-canvas',
      script: 'node_modules/.bin/next',
      args: 'start',
      cwd: '/opt/pixel-canvas',
      env: { NODE_ENV: 'production', PORT: 3000 },
      max_memory_restart: '300M',
    },
    {
      name: 'pixel-socket',
      script: 'node_modules/.bin/ts-node',
      args: '--project tsconfig.server.json server/socket.ts',
      cwd: '/opt/pixel-canvas',
      env: { NODE_ENV: 'production' },
      max_memory_restart: '100M',
    },
  ],
};
PMEOF

cd $APP_DIR
pm2 start ecosystem.config.js
pm2 save
pm2 startup

# Nginx SSL 配置（假设证书已上传）
mkdir -p /etc/nginx/ssl

cat > /etc/nginx/sites-available/pixel-canvas << NGEOF
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${DOMAIN} www.${DOMAIN};

    ssl_certificate /etc/nginx/ssl/${DOMAIN}.pem;
    ssl_certificate_key /etc/nginx/ssl/${DOMAIN}.key;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /socket.io/ {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
NGEOF

ln -sf /etc/nginx/sites-available/pixel-canvas /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ""
echo "============================================"
echo "  部署完成！"
echo "============================================"
echo ""
echo "  网站地址: https://${DOMAIN}"
echo "  管理后台: https://${DOMAIN}/zh-CN/admin"
echo "  管理密码: ${ADMIN_PASSWORD}"
echo ""
echo "  数据库密码: ${DB_PASSWORD}"
echo "  JWT密钥: ${JWT_SECRET}"
echo "  (以上密码请妥善保管)"
echo ""
echo "  SSL 证书需要放到以下位置:"
echo "    /etc/nginx/ssl/${DOMAIN}.pem"
echo "    /etc/nginx/ssl/${DOMAIN}.key"
echo ""
echo "  上传证书命令（在本地执行）:"
echo "    scp ${DOMAIN}.pem root@服务器IP:/etc/nginx/ssl/"
echo "    scp ${DOMAIN}.key root@服务器IP:/etc/nginx/ssl/"
echo "    然后在服务器执行: nginx -t && systemctl reload nginx"
echo ""
echo "  常用命令:"
echo "    pm2 status          # 查看服务状态"
echo "    pm2 logs            # 查看日志"
echo "    pm2 restart all     # 重启服务"
echo ""
