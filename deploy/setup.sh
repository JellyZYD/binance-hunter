#!/usr/bin/env bash
set -euo pipefail

# Ubuntu one-shot deploy.
# Usage:
#   sudo DOMAIN=example.com bash deploy/setup.sh
#   sudo INSTALL_FRONTEND=0 bash deploy/setup.sh

REPO_URL="${REPO_URL:-https://github.com/JellyZYD/binance-hunter.git}"
CLONE_DIR="${CLONE_DIR:-/opt/binance-hunter}"
APP_DIR="${APP_DIR:-${CLONE_DIR}/frontend}"
BACKEND_DIR="${BACKEND_DIR:-${CLONE_DIR}/backend}"
DOMAIN="${DOMAIN:-}"
INSTALL_FRONTEND="${INSTALL_FRONTEND:-1}"
NODE_MAJOR="${NODE_MAJOR:-20}"
HUNTER_TOP="${HUNTER_TOP:-120}"
HUNTER_BROAD_TOP="${HUNTER_BROAD_TOP:-220}"
HUNTER_MAX_WORKERS="${HUNTER_MAX_WORKERS:-8}"
HUNTER_DISCOVER_EVERY="${HUNTER_DISCOVER_EVERY:-15m}"
HUNTER_API_PORT="${HUNTER_API_PORT:-8787}"
NEXT_PORT="${NEXT_PORT:-3000}"
HUNTER_NETWORK_PROXY="${HUNTER_NETWORK_PROXY:-}"
WECOM_WEBHOOK_URL="${WECOM_WEBHOOK_URL:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/setup.sh" >&2
  exit 1
fi

echo "[1/7] Install system packages"
apt-get update
apt-get install -y ca-certificates curl git nginx python3 python3-venv python3-pip sqlite3

if [ "$INSTALL_FRONTEND" = "1" ] && ! command -v node >/dev/null 2>&1; then
  echo "[2/7] Install Node.js ${NODE_MAJOR}"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
else
  echo "[2/7] Node.js install skipped"
fi

echo "[3/7] Clone or update repo"
if [ -d "${CLONE_DIR}/.git" ]; then
  git -C "$CLONE_DIR" fetch origin
  git -C "$CLONE_DIR" reset --hard origin/main
else
  rm -rf "$CLONE_DIR"
  git clone "$REPO_URL" "$CLONE_DIR"
fi

echo "[4/7] Configure backend"
cd "$BACKEND_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p storage alerts data/cache reports

cat >/etc/binance-hunter.env <<EOF
HUNTER_NETWORK_PROXY=${HUNTER_NETWORK_PROXY}
HUNTER_DB_PATH=${BACKEND_DIR}/storage/hunter.db
HUNTER_ALERTS_DIR=${BACKEND_DIR}/alerts
WECOM_WEBHOOK_URL=${WECOM_WEBHOOK_URL}
EOF
chmod 600 /etc/binance-hunter.env

cat >/etc/systemd/system/binance-hunter-monitor.service <<EOF
[Unit]
Description=Binance pump-dump hunter monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${BACKEND_DIR}
EnvironmentFile=/etc/binance-hunter.env
ExecStart=${BACKEND_DIR}/.venv/bin/python run.py monitor --config config/settings.json --top ${HUNTER_TOP} --broad-top ${HUNTER_BROAD_TOP} --discover-every ${HUNTER_DISCOVER_EVERY} --max-workers ${HUNTER_MAX_WORKERS}
Restart=always
RestartSec=5
User=root
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/binance-hunter-api.service <<EOF
[Unit]
Description=Binance pump-dump hunter read-only API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${BACKEND_DIR}
EnvironmentFile=/etc/binance-hunter.env
ExecStart=${BACKEND_DIR}/.venv/bin/python run.py web --config config/settings.json --host 127.0.0.1 --port ${HUNTER_API_PORT}
Restart=always
RestartSec=5
User=root
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

echo "[5/7] Configure frontend"
if [ "$INSTALL_FRONTEND" = "1" ]; then
  cd "$APP_DIR"
  cat >.env <<EOF
HUNTER_API_BASE_URL=http://127.0.0.1:${HUNTER_API_PORT}
NEXT_PUBLIC_APP_URL=${DOMAIN:+https://${DOMAIN}}
EOF
  npm ci
  npm run build

  cat >/etc/systemd/system/binance-hunter-web.service <<EOF
[Unit]
Description=Binance pump-dump hunter web dashboard
After=network-online.target binance-hunter-api.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=NODE_ENV=production
Environment=PORT=${NEXT_PORT}
Environment=HUNTER_API_BASE_URL=http://127.0.0.1:${HUNTER_API_PORT}
ExecStart=/usr/bin/npm run start
Restart=always
RestartSec=5
User=root
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
else
  rm -f /etc/systemd/system/binance-hunter-web.service
fi

echo "[6/7] Configure nginx"
if [ -n "$DOMAIN" ]; then
  cat >/etc/nginx/sites-available/binance-hunter <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /hunter-api/ {
        # 前端 route.ts 会拼成 ${HUNTER_API_BASE_URL}/api/<端点>，
        # 这里转发到后端根路径，使 /hunter-api/api/summary -> 后端 /api/summary。
        proxy_pass http://127.0.0.1:${HUNTER_API_PORT}/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /health {
        proxy_pass http://127.0.0.1:${HUNTER_API_PORT}/health;
    }
EOF
  if [ "$INSTALL_FRONTEND" = "1" ]; then
    cat >>/etc/nginx/sites-available/binance-hunter <<EOF

    location / {
        proxy_pass http://127.0.0.1:${NEXT_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
EOF
  fi
  cat >>/etc/nginx/sites-available/binance-hunter <<'EOF'
}
EOF
  ln -sf /etc/nginx/sites-available/binance-hunter /etc/nginx/sites-enabled/binance-hunter
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx
else
  echo "DOMAIN is empty; nginx public site config skipped"
fi

echo "[7/7] Start services"
systemctl daemon-reload
systemctl enable --now binance-hunter-api.service binance-hunter-monitor.service
if [ "$INSTALL_FRONTEND" = "1" ]; then
  systemctl enable --now binance-hunter-web.service
fi

systemctl --no-pager status binance-hunter-api.service || true
systemctl --no-pager status binance-hunter-monitor.service || true
if [ "$INSTALL_FRONTEND" = "1" ]; then
  systemctl --no-pager status binance-hunter-web.service || true
fi

echo "Deploy complete"
echo "Backend API: http://127.0.0.1:${HUNTER_API_PORT}"
if [ -n "$DOMAIN" ]; then
  echo "Public API for Vercel: http://${DOMAIN}/hunter-api"
  if [ "$INSTALL_FRONTEND" = "1" ]; then
    echo "Dashboard: http://${DOMAIN}/"
  fi
fi
