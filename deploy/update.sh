#!/usr/bin/env bash
set -euo pipefail

# Update an existing server deployment.
# Usage:
#   sudo bash deploy/update.sh

CLONE_DIR="${CLONE_DIR:-/opt/binance-hunter}"
APP_DIR="${APP_DIR:-${CLONE_DIR}/frontend}"
BACKEND_DIR="${BACKEND_DIR:-${CLONE_DIR}/backend}"
INSTALL_FRONTEND="${INSTALL_FRONTEND:-1}"
VERIFY_LIVE="${VERIFY_LIVE:-1}"
LIVE_MAX_NOTIONAL_USDT="${LIVE_MAX_NOTIONAL_USDT:-21}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/update.sh" >&2
  exit 1
fi

echo "[1/5] Pull latest code"
git -C "$CLONE_DIR" fetch origin
git -C "$CLONE_DIR" reset --hard origin/main

echo "[2/5] Update backend"
cd "$BACKEND_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
if [ -f /etc/binance-hunter-live.env ] \
  && systemctl cat binance-hunter-live.service >/dev/null 2>&1; then
  cd "$CLONE_DIR"
  "$BACKEND_DIR/.venv/bin/python" deploy/build-live-server-config.py \
    --max-notional-usdt "$LIVE_MAX_NOTIONAL_USDT"
  install -m 0644 deploy/systemd/binance-hunter-live.service \
    /etc/systemd/system/binance-hunter-live.service
fi

echo "[3/5] Update frontend"
if [ "$INSTALL_FRONTEND" = "1" ]; then
  cd "$APP_DIR"
  npm ci
  npm run build
fi

echo "[4/5] Restart services"
systemctl daemon-reload
systemctl restart binance-hunter-api.service
systemctl restart binance-hunter-monitor.service
if systemctl cat binance-hunter-micro.service >/dev/null 2>&1; then
  systemctl restart binance-hunter-micro.service
fi
if systemctl cat binance-hunter-live.service >/dev/null 2>&1; then
  systemctl restart binance-hunter-live.service
fi
if [ "$INSTALL_FRONTEND" = "1" ] && systemctl list-unit-files binance-hunter-web.service >/dev/null 2>&1; then
  systemctl restart binance-hunter-web.service
fi

systemctl --no-pager --failed || true
if [ "$VERIFY_LIVE" = "1" ]; then
  echo "[5/5] Verify live strategy"
  cd "$CLONE_DIR"
  "$BACKEND_DIR/.venv/bin/python" deploy/verify-live.py
fi
echo "Update complete"
