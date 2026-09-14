#!/usr/bin/env bash
# Install MGX ARC GUI on an ITSS Linux VM (Ubuntu/RHEL-style, systemd).
# Run as a user with sudo:  bash deploy/install_on_vm.sh
#
# Production layout: gunicorn on 127.0.0.1:4281, nginx on :443 (see nginx example).
set -euo pipefail

if grep -q $'\r' "$0" 2>/dev/null; then
  tmp="$(mktemp)"
  tr -d '\r' < "$0" > "$tmp"
  chmod +x "$tmp"
  exec bash "$tmp" "$@"
fi

APP_DIR="${APP_DIR:-/opt/mgx-arc-gui}"
SERVICE_USER="${SERVICE_USER:-$USER}"
PORT="${PORT:-4281}"
BIND="${BIND:-127.0.0.1}"
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

if [[ ! -f "$SRC_DIR/app.py" || ! -f "$SRC_DIR/requirements.txt" ]]; then
  echo "ERROR: '$SRC_DIR' is not the MGX ARC GUI project." >&2
  echo "Re-run with the project path, e.g.:" >&2
  echo "  SRC_DIR=\"/opt/mgx-arc-gui\" bash $0" >&2
  exit 1
fi

echo "==> Installing MGX ARC GUI (ITSS VM)"
echo "    Source:  $SRC_DIR"
echo "    Target:  $APP_DIR"
echo "    User:    $SERVICE_USER"
echo "    Bind:    ${BIND}:${PORT}"

sudo mkdir -p "$APP_DIR"
if command -v rsync >/dev/null 2>&1; then
  sudo rsync -a --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude '.env' \
    "$SRC_DIR/" "$APP_DIR/"
else
  sudo find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name '.venv' ! -name '.env' -exec rm -rf {} +
  sudo cp -a "$SRC_DIR"/. "$APP_DIR"/
fi

sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
  if [[ -f "$APP_DIR/.env.example" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "    Created $APP_DIR/.env from .env.example — edit before production use."
  fi
fi

cd "$APP_DIR"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install "gunicorn>=22.0.0"

UNIT="/tmp/mgx-arc-gui.service"
tr -d '\r' < "$APP_DIR/deploy/mgx-arc-gui.service" | sed \
  -e "s|/opt/mgx-arc-gui|$APP_DIR|g" \
  -e "s|User=nvidia|User=$SERVICE_USER|g" \
  -e "s|Group=nvidia|Group=$SERVICE_USER|g" \
  -e "s|--bind 0.0.0.0:4281|--bind ${BIND}:${PORT}|g" \
  -e "s|Environment=PORT=4281|Environment=PORT=${PORT}|g" \
  > "$UNIT"

sudo cp "$UNIT" /etc/systemd/system/mgx-arc-gui.service

ENV_DROPIN="/etc/systemd/system/mgx-arc-gui.service.d"
if [[ -f "$APP_DIR/.env" ]]; then
  sudo mkdir -p "$ENV_DROPIN"
  printf '%s\n' '[Service]' "EnvironmentFile=${APP_DIR}/.env" | \
    sudo tee "$ENV_DROPIN/env.conf" >/dev/null
fi

sudo systemctl daemon-reload
sudo systemctl enable mgx-arc-gui.service
sudo systemctl restart mgx-arc-gui.service

if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 443/tcp || true
  echo "    ufw: allowed 443/tcp (nginx). Gunicorn is localhost-only."
fi

sleep 1
sudo systemctl --no-pager --full status mgx-arc-gui.service || true

echo
echo "==> Done."
echo "    App:     http://${BIND}:${PORT}/  (localhost; use nginx for HTTPS)"
echo "    Health:  curl -s http://${BIND}:${PORT}/healthz"
echo
echo "Next steps:"
echo "  1. Edit ${APP_DIR}/.env (MGX_ARC_* creds; SSO vars when ITSS app is ready)"
echo "  2. Install nginx TLS proxy: deploy/nginx-mgx-arc.conf.example"
echo "  3. sudo systemctl restart mgx-arc-gui"
echo "  See DEPLOYMENT.md and HANDOFF_NOW.md."
