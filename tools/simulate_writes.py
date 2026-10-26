#!/usr/bin/env python3
"""Generate continuous application traffic against the database so you can watch DMS CDC replicate it.

    python tools/simulate_writes.py                      # 5 ops/sec forever (Ctrl+C to stop)
    python tools/simulate_writes.py --rate 20 --duration 600
    python tools/simulate_writes.py --mix insert=70,update=25,delete=5

Each op is a real transaction: new orders (with items + stock decrement), new customers/products,
order status changes, price/stock changes, and deletes of cancelled orders. A heartbeat row is
written every second; tools/verify_migration.py uses it to measure replication lag.

When you flip the source to read-only (scripts/db_readonly.sh on) the writes start being REJECTED
instead of crashing the simulator, and it prints the last heartbeat id that was committed:
that is the row you wait for on the target before cutting over.
"""
import argparse
import random
import signal
import socket
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from _common import db
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

NEXT_STATUS = {"pending": "paid", "paid": "shipped", "shipped": "delivered"}
FIRST = ["Ama", "Kofi", "Esi", "Yaw", "Maria", "John", "Aisha", "David", "Grace", "Samuel"]
LAST = ["Mensah", "Owusu", "Boateng", "Smith", "Okafor", "Garcia", "Addo", "Brown"]
CITIES = ["Accra", "Kumasi", "Lagos", "London", "Toronto", "Nairobi"]
HOST = socket.gethostname()


def utcnow(micro=False):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now if micro else now.replace(microsecond=0)


def random_id(conn, table, where=""):
    """Pick a random existing id without ORDER BY RAND() (cheap on big tables)."""
    lo, hi = conn.execute(text(f"SELECT MIN(id), MAX(id) FROM {table} {where}")).one()
    if lo is None:
        return None
    pivot = random.randint(lo, hi)
    joiner = "AND" if where else "WHERE"
    return conn.execute(text(f"SELECT id FROM {table} {where} {joiner} id >= :p ORDER BY id LIMIT 1"),
                        {"p": pivot}).scalar()


# ---------------------------------------------------------------- operations

def op_new_order(conn):
    cid = random_id(conn, "customers")
    if cid is None:
        return op_new_customer(conn)
    lines = []
    for _ in range(random.randint(1, 4)):
        pid = random_id(conn, "products")
        if pid is not None:
            lines.append((pid, random.randint(1, 3)))
    if not lines:
        return op_new_product(conn)
    prices = dict(conn.execute(select(db.products.c.id, db.products.c.price)
                               .where(db.products.c.id.in_([p for p, _ in lines]))).all())
    now = utcnow()
    total = sum(prices[p] * q for p, q in lines)
    oid = conn.execute(insert(db.orders).values(customer_id=cid, status="pending", total=total,
                                                created_at=now, updated_at=now)).inserted_primary_key[0]
    for pid, qty in lines:
        conn.execute(insert(db.order_items).values(order_id=oid, product_id=pid, quantity=qty,
                                                   unit_price=prices[pid]))
        conn.execute(update(db.products).where(db.products.c.id == pid)
                     .values(stock=db.products.c.stock - qty, updated_at=now))
    return "insert:order"


def op_new_customer(conn):
    fn, ln = random.choice(FIRST), random.choice(LAST)
    now = utcnow()
    tag = f"{int(time.time() * 1000)}{random.randint(100, 999)}"
    conn.execute(insert(db.customers).values(email=f"{fn}.{ln}.{tag}@sim.example.com".lower(),
                                             full_name=f"{fn} {ln}", city=random.choice(CITIES),
                                             created_at=now, updated_at=now))
    return "insert:customer"


def op_new_product(conn):
    now = utcnow()
    tag = f"{int(time.time() * 1000) % 10**9}{random.randint(10, 99)}"
    conn.execute(insert(db.products).values(sku=f"SIM-{tag}", name=f"Simulated Item {tag}",
                                            description="Created by simulate_writes.py",
                                            price=Decimal(random.randint(199, 19999)) / 100,
                                            stock=random.randint(50, 500), created_at=now, updated_at=now))
    return "insert:product"


def op_advance_order(conn):
    oid = random_id(conn, "orders", "WHERE status IN ('pending','paid','shipped')")
    if oid is None:
        return op_new_order(conn)
    status = conn.execute(select(db.orders.c.status).where(db.orders.c.id == oid)).scalar()
    new = "cancelled" if random.random() < 0.1 else NEXT_STATUS.get(status, "delivered")
    conn.execute(update(db.orders).where(db.orders.c.id == oid).values(status=new, updated_at=utcnow()))
    return "update:order_status"


def op_update_product(conn):
    pid = random_id(conn, "products")
    if pid is None:
        return op_new_product(conn)
    pct = Decimal(random.choice([90, 95, 105, 110])) / 100
    conn.execute(update(db.products).where(db.products.c.id == pid).values(
        price=func.round(db.products.c.price * pct, 2), stock=db.products.c.stock + random.randint(0, 50),
        updated_at=utcnow()))
    return "update:product"


def op_update_customer(conn):
    cid = random_id(conn, "customers")
    if cid is None:
        return op_new_customer(conn)
    conn.execute(update(db.customers).where(db.customers.c.id == cid)
                 .values(city=random.choice(CITIES), updated_at=utcnow()))
    return "update:customer"


def op_delete_order(conn):
    oid = random_id(conn, "orders", "WHERE status = 'cancelled'")
    if oid is None:
        return op_advance_order(conn)
    # Children first: the schema has no ON DELETE CASCADE (cascades never reach the binlog).
    conn.execute(delete(db.order_items).where(db.order_items.c.order_id == oid))
    conn.execute(delete(db.orders).where(db.orders.c.id == oid))
    return "delete:order"


OPS = {
    "insert": [(op_new_order, 80), (op_new_customer, 15), (op_new_product, 5)],
    "update": [(op_advance_order, 60), (op_update_product, 30), (op_update_customer, 10)],
    "delete": [(op_delete_order, 100)],
}


def pick(mix):
    kind = random.choices(list(mix), weights=list(mix.values()))[0]
    fns, weights = zip(*OPS[kind])
    return random.choices(fns, weights=weights)[0]


def parse_mix(s):
    mix = {}
    for part in s.split(","):
        k, v = part.split("=")
        if k.strip() not in OPS:
            raise argparse.ArgumentTypeError(f"unknown op kind {k!r}; use insert/update/delete")
        mix[k.strip()] = int(v)
    return mix


# ---------------------------------------------------------------- main loop

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=float, default=5, help="operations per second (default 5)")
    ap.add_argument("--duration", type=int, default=0, help="seconds to run, 0 = until Ctrl+C")
    ap.add_argument("--mix", type=parse_mix, default="insert=55,update=38,delete=7",
                    help="weights per op kind (default insert=55,update=38,delete=7)")
    ap.add_argument("--no-heartbeat", action="store_true", help="don't write heartbeat rows")
    ap.add_argument("--report", type=int, default=5, help="seconds between status lines")
    ap.add_argument("--env-prefix", default="DB_", help="which env vars to use (default DB_)")
    args = ap.parse_args()
    if isinstance(args.mix, str):
        args.mix = parse_mix(args.mix)

    engine = db.make_engine(args.env_prefix, pool_size=2)
    cfg = engine.shop_config
    print(f"Writing to {cfg['engine']} {cfg['host']}:{cfg['port']}/{cfg['name']} at {args.rate}/s, mix={args.mix}")
    print("Ctrl+C to stop.\n")

    stats, window = Counter(), Counter()
    last_error, last_hb_id, last_hb_at = "", None, None
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def run(fn):
        nonlocal last_error
        try:
            with engine.begin() as conn:
                name = fn(conn)
            stats[name] += 1
            window["ok"] += 1
            return True
        except SQLAlchemyError as exc:
            if db.is_read_only_error(exc):
                stats["rejected:read_only"] += 1
                window["rejected"] += 1
            elif isinstance(exc, (OperationalError, DBAPIError)) and exc.connection_invalidated:
                stats["error:connection"] += 1
                window["error"] += 1
                time.sleep(1)
            else:
                stats["error:other"] += 1
                window["error"] += 1
            last_error = str(getattr(exc, "orig", exc)).splitlines()[0][:160]
            return False

    def heartbeat(conn):
        nonlocal last_hb_id, last_hb_at
        at = utcnow(micro=True)
        hb_id = conn.execute(insert(db.heartbeat).values(source_host=HOST, written_at=at)).inserted_primary_key[0]
        last_hb_id, last_hb_at = hb_id, at
        return "insert:heartbeat"

    started = time.monotonic()
    next_op = next_hb = next_report = started
    interval = 1.0 / args.rate if args.rate > 0 else None
    while not stopping:
        now = time.monotonic()
        if args.duration and now - started >= args.duration:
            break
        if not args.no_heartbeat and now >= next_hb:
            run(heartbeat)
            next_hb = now + 1
        if interval and now >= next_op:
            run(pick(args.mix))
            next_op += interval
            if next_op < now - 1:      # fell behind (slow DB); don't burst to catch up
                next_op = now
        if now >= next_report:
            if next_report > started:
                state = "READ-ONLY (writes rejected)" if window["rejected"] and not window["ok"] else "writing"
                hb = f"last heartbeat id={last_hb_id} at {last_hb_at:%H:%M:%S.%f}"[:-3] if last_hb_id else "no heartbeat yet"
                print(f"[{time.strftime('%H:%M:%S')}] {state:<27} ok={window['ok']:<4} rejected={window['rejected']:<4} "
                      f"errors={window['error']:<3} | {hb}" + (f" | {last_error}" if window["error"] or window["rejected"] else ""),
                      flush=True)
            window.clear()
            next_report = now + args.report
        time.sleep(0.01)

    print("\n==== summary ====")
    for k, v in sorted(stats.items()):
        print(f"  {k:<24} {v:>8,}")
    if last_hb_id:
        print(f"\nLast COMMITTED heartbeat on source: id={last_hb_id} written_at={last_hb_at} UTC")
        print("Before cutover, wait until the target has this heartbeat id (tools/verify_migration.py).")


if __name__ == "__main__":
    main()
