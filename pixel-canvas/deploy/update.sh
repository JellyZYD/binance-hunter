#!/bin/bash
# 快速更新部署
# 用法: bash deploy/update.sh

set -e
APP_DIR="/opt/pixel-canvas/pixel-canvas"

echo "=== 更新部署 ==="
cd $APP_DIR

# 拉取最新代码
cd /opt/pixel-canvas
git pull origin main
cd $APP_DIR

npm install --production=false
npm install ts-node
npx prisma generate
npx prisma db push --accept-data-loss
npm run build

pm2 restart all
echo "=== 更新完成！==="
