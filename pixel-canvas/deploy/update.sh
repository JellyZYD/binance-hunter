#!/bin/bash
# 快速更新部署
# 用法: bash deploy/update.sh

set -e
APP_DIR="/opt/pixel-canvas/pixel-canvas"

echo "=== 更新部署 ==="
cd $APP_DIR

# 拉取最新代码（如果用 git）
# git pull origin main

# 或者从本地同步
rsync -av --exclude='node_modules' --exclude='.next' --exclude='.git' \
  ./ $APP_DIR/

cd $APP_DIR
npm install --production=false
npx prisma generate
npx prisma db push --accept-data-loss
npm run build

pm2 restart all
echo "=== 更新完成！==="
