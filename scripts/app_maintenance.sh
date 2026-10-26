#!/usr/bin/env bash
# Toggle the app's own maintenance (read-only) mode on the web server.
# Users can still browse; forms show a "migration in progress" message instead of writing.
#
#   sudo ./scripts/app_maintenance.sh on|off
set -euo pipefail
ENV_FILE=${ENV_FILE:-/etc/shopapp.env}

case "${1:-}" in
  on)  value=true ;;
  off) value=false ;;
  *)   echo "usage: $0 on|off" >&2; exit 2 ;;
esac

if grep -q '^APP_READ_ONLY=' "$ENV_FILE"; then
  sed -i "s/^APP_READ_ONLY=.*/APP_READ_ONLY=$value/" "$ENV_FILE"
else
  echo "APP_READ_ONLY=$value" >> "$ENV_FILE"
fi
systemctl restart shopapp
echo "APP_READ_ONLY=$value (shopapp restarted)"
