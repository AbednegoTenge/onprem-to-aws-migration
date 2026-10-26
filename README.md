# ShopLite: an on-prem → AWS migration lab

A small order-management website (Flask + MySQL) built for practising:

- **AWS MGN**: rehost the web server onto EC2.
- **AWS SCT**: convert the MySQL schema to PostgreSQL.
- **AWS DMS**: full load + CDC from on-prem MySQL into RDS for PostgreSQL.
- **Cutover**: freeze the source in read-only mode, let CDC drain, verify, and switch the app over.

The same app code runs against MySQL **or** PostgreSQL. You switch it by editing one env file.

```
┌──────────── on-prem ────────────┐               ┌──────────────── AWS ────────────────┐
│ web server (nginx+gunicorn) ────┼── MGN ───────▶│ EC2 (replicated web server)         │
│        │                        │               │        │ after cutover               │
│        ▼                        │               │        ▼                             │
│ MySQL 8.0 shopdb (binlog=ROW) ──┼── DMS (full ─▶│ RDS PostgreSQL  db=shopdb           │
│        ▲                        │   load + CDC) │                 schema=shopdb (SCT)  │
│ tools/simulate_writes.py        │               │                                      │
└─────────────────────────────────┘               └──────────────────────────────────────┘
```

## What's in here

| Path | What it is |
|---|---|
| `app/` | The website: dashboard, orders, customers, products, `/health` (for ALB), `/api/stats` |
| `db/mysql_schema.sql` | Source schema: 5 tables, a view and a stored procedure, with some MySQL-specific features for SCT to convert |
| `db/mysql_users.sql` | `shopapp` (app user) and `dms_user` (binlog/replication privileges for DMS + SCT) |
| `db/mysql_cdc.cnf` | MySQL settings DMS CDC needs (`binlog_format=ROW`, `binlog_row_image=FULL`, …) |
| `db/pg_post_cutover.sql` | Run on RDS before cutover: resets sequences + grants for the app user |
| `tools/seed.py` | Loads bulk data so the full load has something to move |
| `tools/simulate_writes.py` | Keeps sending inserts/updates/deletes + a 1-per-second heartbeat, for CDC practice |
| `tools/verify_migration.py` | Compares source vs target (counts, checksums, row-by-row, replication lag) |
| `scripts/setup_db_server.sh` | Installs MySQL and applies the schema, users and CDC config |
| `scripts/setup_web_server.sh` | Installs the app under nginx + gunicorn + systemd |
| `scripts/db_readonly.sh` | `on` / `off` / `status`: freeze the source for cutover |
| `scripts/app_maintenance.sh` | `on` / `off`: app-level maintenance mode (forms disabled, browsing still works) |

The header bar on every page shows **which DB engine and host** the app is using, the **web hostname / EC2 instance id**, and whether the DB is **read-only**. That makes each step of the migration visible in the browser.

---

## 1. Build the "on-prem" environment

Ubuntu 22.04 or 24.04. Use one VM for both roles, or two VMs (web + db). Copy this folder to each server:

```bash
scp -r onprem-shop-migration ubuntu@<server>:~
```

**Database server**

```bash
cd ~/onprem-shop-migration
sudo APP_DB_PASSWORD='App-Pass-123' DMS_DB_PASSWORD='Dms-Pass-123' ./scripts/setup_db_server.sh
```

**Web server**

```bash
cd ~/onprem-shop-migration
sudo ./scripts/setup_web_server.sh
sudo nano /etc/shopapp.env
sudo systemctl restart shopapp
```

In `/etc/shopapp.env`, set `DB_HOST` (`127.0.0.1` if the DB is on the same box) and `DB_PASSWORD`.

**Seed data.** The defaults give 5k customers, 500 products and 20k orders. Raise them for a longer full load:

```bash
sudo /opt/shopapp/venv/bin/python /opt/shopapp/tools/seed.py --customers 50000 --orders 200000
```

Browse to `http://<web-server-ip>/`. The header should show **DB: MySQL @ …** and **DB read/write**.

> The tools read `/etc/shopapp.env` (hence `sudo`). To run them from a laptop instead: `pip install -r requirements.txt`, then `cp .env.example .env` and edit it.

## 2. Rehost the web server with MGN

Install the MGN replication agent on the web server and replicate it as normal. Then launch a test instance and browse to it. The header shows the EC2 **instance id**, so you can tell which copy you're looking at. `/health` returns 200 only when the DB is reachable, so you can use it as the ALB target-group health check.

## 3. Convert the schema with SCT

Connect SCT to MySQL as `dms_user` and to RDS. Convert `shopdb`, which becomes **schema `shopdb`** in your RDS database. Things worth noticing in the assessment report:

| MySQL feature | What SCT does with it |
|---|---|
| `AUTO_INCREMENT` | identity/serial column. The sequence must be reset after the load (step 6). |
| `ENUM` on `orders.status` | varchar + CHECK constraint |
| `TINYINT(1)` `customers.is_active` | smallint or boolean (the app doesn't depend on it) |
| `ON UPDATE CURRENT_TIMESTAMP` | a trigger function on PostgreSQL. **Read the CDC note below.** |
| `v_customer_revenue` view | converted view. The dashboard's "Top customers" box uses it, so a broken conversion shows up there. |
| `sp_customer_summary` | function/procedure conversion (`IFNULL` → `COALESCE`) |

Apply the converted schema to RDS **before** starting DMS. Use DMS table prep mode **Do nothing** or **Truncate**.

## 4. DMS: full load + CDC

- **Source endpoint:** MySQL, user `dms_user`. The binlog prerequisites are already set; check them with `scripts/setup_db_server.sh`'s output or `SHOW VARIABLES LIKE 'binlog%'`.
- **Target endpoint:** PostgreSQL. Add the extra connection attribute `afterConnectScript=SET session_replication_role='replica'`. This turns off FK checks **and triggers** while DMS writes. Without it, the SCT-generated `updated_at` trigger rewrites `updated_at` on every CDC update, and `verify_migration.py --deep` will flag those rows. Try it both ways.
- **Task:** *Migrate existing data and replicate ongoing changes*, table mapping `shopdb.%`. `products.description` is `TEXT`, which DMS treats as a LOB. Limited LOB mode works fine here.

Start the write simulator **before** you start the task. That way changes keep arriving during the full load and DMS has to apply the cached changes:

```bash
sudo /opt/shopapp/venv/bin/python /opt/shopapp/tools/simulate_writes.py --rate 10
```

Watch the target catch up. This needs the `SRC_*` and `TGT_*` values filled in:

```bash
sudo /opt/shopapp/venv/bin/python /opt/shopapp/tools/verify_migration.py --watch 5
```

The heartbeat line shows how many heartbeats or seconds the target is behind. That is your end-to-end CDC lag, and you can compare it with the task's `CDCLatencySource` / `CDCLatencyTarget` metrics.

## 5. Cutover

```bash
# (optional) stop users writing first: forms show "migration in progress"
sudo ./scripts/app_maintenance.sh on

# freeze the source; prints the final binlog position + last heartbeat id
sudo ./scripts/db_readonly.sh on
```

The simulator keeps running but now prints `READ-ONLY (writes rejected)`. Every page shows a read-only banner. Stop the simulator with Ctrl+C. It prints the **last committed heartbeat id**.

Wait until `verify_migration.py` reports `IN SYNC` and the target has that heartbeat id. Then do a final full check:

```bash
sudo /opt/shopapp/venv/bin/python /opt/shopapp/tools/verify_migration.py --deep
```

`RESULT: MATCH` means you're good. Stop the DMS task.

## 6. Switch the app to RDS

```bash
psql "host=<rds-endpoint> dbname=shopdb user=postgres" -c "CREATE USER shopapp WITH PASSWORD 'App-Pass-123';"
psql "host=<rds-endpoint> dbname=shopdb user=postgres" -f db/pg_post_cutover.sql
```

`pg_post_cutover.sql` does two things:

- **Resets the sequences.** You can skip it to see the failure: the first new order fails with `duplicate key value violates unique constraint "orders_pkey"`. That happens because DMS copied rows with their ids but never moved the sequences forward.
- **Grants table access** to `shopapp`.

On the (MGN-migrated) web server, edit `/etc/shopapp.env`: comment out the MySQL `DB_*` block, uncomment the PostgreSQL block, and set `APP_READ_ONLY=false`. Then restart:

```bash
sudo systemctl restart shopapp
```

The header should now say **DB: PostgreSQL @ <rds-endpoint>**. Run the simulator against RDS to show writes work after cutover:

```bash
sudo /opt/shopapp/venv/bin/python /opt/shopapp/tools/simulate_writes.py --rate 5 --duration 60
```

**Rollback drill:** before any writes go to RDS, run `sudo ./scripts/db_readonly.sh off` and point the env file back at MySQL.

---

## Gotchas this lab is designed to show you

1. **Every table needs a primary key.** DMS can't apply CDC updates or deletes without one. All tables here have one.
2. **No `ON DELETE CASCADE`.** InnoDB cascaded deletes are never written to the binlog, so DMS can't see them. The app and simulator delete child rows explicitly.
3. **Sequences don't come across.** Fix them with `pg_post_cutover.sql`.
4. **Triggers fire during CDC** unless you set `session_replication_role=replica`. See step 4.
5. **`read_only` vs `super_read_only`.** Plain `read_only` still lets users with SUPER write. The script uses `super_read_only`. It is set with `SET GLOBAL`, so restarting MySQL undoes it.
6. **Making RDS read-only** (for a reverse-cutover drill): `ALTER DATABASE shopdb SET default_transaction_read_only = on;` also locks *your* admin session. To undo it, run the two statements as separate transactions:
   `psql ... -c "SET default_transaction_read_only = off" -c "ALTER DATABASE shopdb SET default_transaction_read_only = off"`.
7. **Binlog retention.** If a DMS task stays stopped longer than `binlog_expire_logs_seconds` (24h here), it can't resume CDC.
8. **Timestamps** are written by the app in UTC, so source and target compare cleanly.

## Run locally (optional)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # point DB_* at a MySQL you can reach
venv/bin/python app/app.py   # http://localhost:8000
```
