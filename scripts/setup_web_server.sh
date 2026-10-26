#!/usr/bin/env bash
# Install the ShopLite web app behind nginx + gunicorn (Ubuntu 22.04 / 24.04).
# This is the server you will replicate with AWS MGN.
#
#   sudo ./scripts/setup_web_server.sh
#   sudo nano /etc/shopapp.env        # set DB_HOST / DB_PASSWORD
#   sudo systemctl restart shopapp
#
# Re-running it updates the code and keeps /etc/shopapp.env.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
APP_DIR=/opt/shopapp

echo "==> Installing packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip nginx rsync mysql-client postgresql-client

echo "==> Copying app to $APP_DIR"
id shopapp &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin shopapp
mkdir -p "$APP_DIR"
rsync -a --delete --exclude venv --exclude .env --exclude __pycache__ --exclude terraform --exclude .git ./ "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [[ ! -f /etc/shopapp.env ]]; then
  echo "==> Creating /etc/shopapp.env from .env.example (edit it!)"
  install -m 640 -o root -g shopapp .env.example /etc/shopapp.env
  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')/" /etc/shopapp.env
fi
chown -R root:shopapp "$APP_DIR"

echo "==> systemd + nginx"
install -m 644 deploy/shopapp.service /etc/systemd/system/shopapp.service
install -m 644 deploy/nginx-shopapp.conf /etc/nginx/sites-available/shopapp
ln -sf /etc/nginx/sites-available/shopapp /etc/nginx/sites-enabled/shopapp
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now shopapp
systemctl restart shopapp nginx

sleep 2
echo "==> Health check"
curl -s http://127.0.0.1/health || true
echo
echo "Done. Browse to http://<this-server-ip>/  (logs: journalctl -u shopapp -f)"
