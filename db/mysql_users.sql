-- Placeholders are replaced by scripts/setup_db_server.sh.
-- Run manually: replace __APP_PASSWORD__ / __DMS_PASSWORD__ first.

-- Application user: normal read/write, NO SUPER, so super_read_only blocks it.
CREATE USER IF NOT EXISTS 'shopapp'@'%' IDENTIFIED BY '__APP_PASSWORD__';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE ON shopdb.* TO 'shopapp'@'%';

-- DMS source user. DMS needs to read the binlog for CDC.
-- mysql_native_password keeps it compatible with every DMS engine version.
CREATE USER IF NOT EXISTS 'dms_user'@'%' IDENTIFIED WITH mysql_native_password BY '__DMS_PASSWORD__';
GRANT SELECT, SHOW VIEW, EXECUTE ON shopdb.* TO 'dms_user'@'%';
GRANT REPLICATION CLIENT, REPLICATION SLAVE ON *.* TO 'dms_user'@'%';

-- SCT also connects with dms_user; it needs to read stored procedure bodies.
GRANT SHOW_ROUTINE ON *.* TO 'dms_user'@'%';

FLUSH PRIVILEGES;
