#!/usr/bin/env bash
# Install and configure the on-prem MySQL 8.0 source (Ubuntu 22.04 / 24.04).
#
#   sudo APP_DB_PASSWORD='App-Pass-123' DMS_DB_PASSWORD='Dms-Pass-123' ./scripts/setup_db_server.sh
#
# Avoid single quotes in the passwords (they end up inside SQL string literals).
set -euo pipefail
cd "$(dirname "$0")/.."

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
: "${APP_DB_PASSWORD:?set APP_DB_PASSWORD}"
: "${DMS_DB_PASSWORD:?set DMS_DB_PASSWORD}"

echo "==> Installing MySQL server"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y mysql-server

echo "==> Enabling binlog / CDC settings for DMS"
install -m 644 db/mysql_cdc.cnf /etc/mysql/mysql.conf.d/zz-migration.cnf
systemctl restart mysql
systemctl enable mysql

echo "==> Creating schema shopdb"
mysql < db/mysql_schema.sql

echo "==> Creating users shopapp and dms_user"
esc() { printf '%s' "$1" | sed -e 's/[\/&\\]/\\&/g'; }
sed -e "s/__APP_PASSWORD__/$(esc "$APP_DB_PASSWORD")/" \
    -e "s/__DMS_PASSWORD__/$(esc "$DMS_DB_PASSWORD")/" db/mysql_users.sql | mysql

echo "==> Verifying CDC prerequisites"
mysql -t -e "SHOW VARIABLES WHERE Variable_name IN
  ('log_bin','binlog_format','binlog_row_image','binlog_checksum','server_id','bind_address','binlog_expire_logs_seconds','version');"

cat <<EOF

MySQL is ready.
  * Open TCP 3306 from the web server, the DMS replication instance and the SCT machine.
  * Next: install the web app (scripts/setup_web_server.sh), then seed data:
        /opt/shopapp/venv/bin/python /opt/shopapp/tools/seed.py
EOF
