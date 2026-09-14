#!/usr/bin/env bash
# Install MGX ARC GUI on a DGX Spark (Ubuntu/DGX OS) as a systemd service.
# Run as a user with sudo:  bash deploy/install_on_spark.sh
set -euo pipefail

# Strip Windows CRLF if this file was copied from Windows.
if grep -q $'\r' "$0" 2>/dev/null; then
  tmp="$(mktemp)"
  tr -d '\r' < "$0" > "$tmp"
  chmod +x "$tmp"
  exec bash "$tmp" "$@"
fi

APP_DIR="${APP_DIR:-/opt/mgx-arc-gui}"
SERVICE_USER="${SERVICE_USER:-$USER}"
PORT="${PORT:-4281}"
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# Guard: running the script from a copied location (e.g. /tmp) would otherwise
# resolve SRC_DIR to a directory that is not the project (worst case "/").
if [[ ! -f "$SRC_DIR/app.py" || ! -f "$SRC_DIR/requirements.txt" ]]; then
  echo "ERROR: '$SRC_DIR' is not the MGX ARC GUI project." >&2
  echo "Re-run with the project path, e.g.:" >&2
  echo "  SRC_DIR=\"\$HOME/MGXARC-GUI-RIDDHI\" bash $0" >&2
  exit 1
fi

echo "==> Installing MGX ARC GUI"
echo "    Source:  $SRC_DIR"
echo "    Target:  $APP_DIR"
echo "    User:    $SERVICE_USER"
echo "    Port:    $PORT"

sudo mkdir -p "$APP_DIR"
if command -v rsync >/dev/null 2>&1; then
  sudo rsync -a --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    "$SRC_DIR/" "$APP_DIR/"
else
  sudo find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name '.venv' -exec rm -rf {} +
  sudo cp -a "$SRC_DIR"/. "$APP_DIR"/
fi

sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

cd "$APP_DIR"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install "gunicorn>=22.0.0"

# Patch systemd unit with this user / port / path
UNIT="/tmp/mgx-arc-gui.service"
tr -d '\r' < "$APP_DIR/deploy/mgx-arc-gui.service" | sed \
  -e "s|/opt/mgx-arc-gui|$APP_DIR|g" \
  -e "s|User=nvidia|User=$SERVICE_USER|g" \
  -e "s|Group=nvidia|Group=$SERVICE_USER|g" \
  -e "s|4281|$PORT|g" \
  > "$UNIT"

sudo cp "$UNIT" /etc/systemd/system/mgx-arc-gui.service
sudo systemctl daemon-reload
sudo systemctl enable mgx-arc-gui.service
sudo systemctl restart mgx-arc-gui.service

# Open firewall if ufw is active
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow "${PORT}/tcp" || true
fi

sleep 1
sudo systemctl --no-pager --full status mgx-arc-gui.service || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "==> Done."
echo "    Local:   http://127.0.0.1:${PORT}/"
echo "    LAN:     http://${IP:-<spark-ip>}:${PORT}/"
echo
echo "Remote access (pick one):"
echo "  1) NVIDIA VPN  → open http://<spark-corp-ip>:${PORT}/ from home/HQ"
echo "  2) Tailscale   → install on Spark, then open http://<tailscale-ip>:${PORT}/ from anywhere"
echo "  See deploy/REMOTE_ACCESS.md for details."
