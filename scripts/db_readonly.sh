#!/usr/bin/env bash
# Freeze / unfreeze writes on the on-prem MySQL source for the cutover.
#
#   sudo ./scripts/db_readonly.sh on       # stop all writes, print final binlog position
#   sudo ./scripts/db_readonly.sh status
#   sudo ./scripts/db_readonly.sh off      # roll back: make the source writable again
#
# super_read_only blocks every user, including ones with SUPER. DMS keeps reading the binlog,
# so CDC drains the last changes to RDS while nothing new arrives.
# SET GLOBAL (not PERSIST): a MySQL restart puts the server back in read/write.
#
# Remote use: MYSQL="mysql -h 10.0.0.5 -u admin -p" ./scripts/db_readonly.sh on
set -euo pipefail
MYSQL=${MYSQL:-mysql}

binlog_status() {
  # MySQL 8.4 renamed SHOW MASTER STATUS
  $MYSQL -t -e "SHOW BINARY LOG STATUS" 2>/dev/null || $MYSQL -t -e "SHOW MASTER STATUS"
}

show() {
  $MYSQL -t -e "SELECT @@global.read_only AS read_only, @@global.super_read_only AS super_read_only, NOW() AS server_time;"
  binlog_status
  $MYSQL -t -e "SELECT MAX(id) AS last_heartbeat_id, MAX(written_at) AS last_heartbeat_at FROM shopdb.heartbeat;"
}

case "${1:-status}" in
  on)
    $MYSQL -e "SET GLOBAL super_read_only = ON;"
    echo "Source is now READ-ONLY. Final state:"
    show
    cat <<'EOF'
Next:
  1. Watch the DMS task until CDC latency is ~0 and the target has last_heartbeat_id above:
       python tools/verify_migration.py --watch 5
  2. Run db/pg_post_cutover.sql on RDS (resets sequences).
  3. Point the app at RDS (/etc/shopapp.env -> DB_ENGINE=postgres ...) and restart it.
EOF
    ;;
  off)
    $MYSQL -e "SET GLOBAL super_read_only = OFF; SET GLOBAL read_only = OFF;"
    echo "Source is READ/WRITE again."
    show
    ;;
  status)
    show
    ;;
  *)
    echo "usage: $0 on|off|status" >&2; exit 2 ;;
esac
