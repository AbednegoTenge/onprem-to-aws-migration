"""Database access shared by the web app and the tools.

The same code talks to the on-prem MySQL source and the RDS PostgreSQL target.
Which one is chosen purely by environment variables, so cutover is just an
edit to /etc/shopapp.env followed by `systemctl restart shopapp`.

    <PREFIX>ENGINE    mysql | postgres
    <PREFIX>HOST, <PREFIX>PORT, <PREFIX>NAME, <PREFIX>USER, <PREFIX>PASSWORD
    <PREFIX>SCHEMA    postgres only; SCT puts MySQL db `shopdb` in schema `shopdb`

The app uses PREFIX="DB_"; verify_migration.py uses "SRC_" and "TGT_".
"""
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

from sqlalchemy import (
    BigInteger, Column, DateTime, ForeignKey, Integer, MetaData, Numeric,
    String, Table, Text, create_engine, text,
)



def _load_env():
    """Env file: $SHOPAPP_ENV_FILE, else /etc/shopapp.env (installed server), else repo .env.
    Real environment variables always win over the file."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.environ.get("SHOPAPP_ENV_FILE"), "/etc/shopapp.env", os.path.join(here, "..", ".env")):
        if path and os.path.isfile(path) and os.access(path, os.R_OK):
            load_dotenv(path, override=False)
            return path
    return None


ENV_FILE = _load_env()
metadata = MetaData()

# Table definitions are only used to build portable INSERT/UPDATE statements.
# The schema itself is owned by db/mysql_schema.sql (source) and SCT (target).
customers = Table(
    "customers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(255), nullable=False),
    Column("full_name", String(150), nullable=False),
    Column("city", String(100)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

products = Table(
    "products", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(40), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("price", Numeric(10, 2), nullable=False),
    Column("stock", Integer, nullable=False),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

orders = Table(
    "orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer_id", Integer, ForeignKey("customers.id"), nullable=False),
    Column("status", String(20), nullable=False),
    Column("total", Numeric(12, 2), nullable=False),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)

order_items = Table(
    "order_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", Integer, ForeignKey("orders.id"), nullable=False),
    Column("product_id", Integer, ForeignKey("products.id"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price", Numeric(10, 2), nullable=False),
)

heartbeat = Table(
    "heartbeat", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("source_host", String(100), nullable=False),
    Column("written_at", DateTime, nullable=False),
)

TABLES = ["customers", "products", "orders", "order_items", "heartbeat"]
ORDER_STATUSES = ["pending", "paid", "shipped", "delivered", "cancelled"]


def config(prefix="DB_"):
    engine = os.environ.get(prefix + "ENGINE", "mysql").lower()
    if engine in ("postgresql", "pg"):
        engine = "postgres"
    return {
        "engine": engine,
        "host": os.environ.get(prefix + "HOST", "127.0.0.1"),
        "port": int(os.environ.get(prefix + "PORT", "5432" if engine == "postgres" else "3306")),
        "name": os.environ.get(prefix + "NAME", "shopdb"),
        "user": os.environ.get(prefix + "USER", "shopapp"),
        "password": os.environ.get(prefix + "PASSWORD", ""),
        "schema": os.environ.get(prefix + "SCHEMA", "shopdb"),
    }


def make_engine(prefix="DB_", **kwargs):
    cfg = config(prefix)
    auth = f"{quote_plus(cfg['user'])}:{quote_plus(cfg['password'])}"
    if cfg["engine"] == "postgres":
        url = f"postgresql+psycopg2://{auth}@{cfg['host']}:{cfg['port']}/{cfg['name']}"
        connect_args = {
            "options": f"-csearch_path={cfg['schema']},public",
            "connect_timeout": 5,
        }
    elif cfg["engine"] == "mysql":
        url = f"mysql+pymysql://{auth}@{cfg['host']}:{cfg['port']}/{cfg['name']}?charset=utf8mb4"
        connect_args = {"connect_timeout": 5}
    else:
        raise ValueError(f"{prefix}ENGINE must be mysql or postgres, got {cfg['engine']!r}")
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=300,
                           connect_args=connect_args, **kwargs)
    engine.shop_config = cfg
    return engine


def is_db_read_only(conn, engine_name):
    """True when the database itself refuses writes (super_read_only / RDS read-only)."""
    if engine_name == "mysql":
        return bool(conn.execute(text("SELECT @@global.read_only")).scalar())
    return conn.execute(text("SHOW transaction_read_only")).scalar() == "on"


def is_read_only_error(exc):
    """Recognise the error a write gets when the database is in read-only mode."""
    msg = str(getattr(exc, "orig", exc)).lower()
    return ("read-only" in msg or "read only" in msg or "1290" in msg  # MySQL ER_OPTION_PREVENTS_STATEMENT
            or "25006" in msg)                                         # PG read_only_sql_transaction
