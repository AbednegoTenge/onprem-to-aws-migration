#!/usr/bin/env python3
"""Fill the on-prem MySQL database with realistic data so the DMS full load has something to move.

    python tools/seed.py                                 # 5k customers, 500 products, 20k orders
    python tools/seed.py --customers 50000 --orders 200000   # a bigger full load
    python tools/seed.py --reset                         # wipe all tables first

Run this against the SOURCE only: it writes explicit ids.
"""
import argparse
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from _common import db
from sqlalchemy import func, insert, select, text

FIRST = ["Ama", "Kofi", "Abena", "Kwame", "Esi", "Yaw", "Akosua", "Kojo", "Efua", "Kwesi", "Maria", "John",
         "Aisha", "David", "Fatima", "James", "Linda", "Samuel", "Grace", "Daniel", "Sarah", "Michael", "Nana", "Joyce"]
LAST = ["Mensah", "Owusu", "Boateng", "Asante", "Osei", "Appiah", "Tenge", "Addo", "Smith", "Johnson",
        "Okafor", "Adeyemi", "Brown", "Garcia", "Mohammed", "Wilson", "Agyeman", "Darko", "Quaye", "Badu"]
CITIES = ["Accra", "Kumasi", "Takoradi", "Tamale", "Cape Coast", "Lagos", "London", "New York", "Toronto",
          "Berlin", "Nairobi", "Johannesburg", None]
ADJ = ["Classic", "Ultra", "Eco", "Pro", "Mini", "Smart", "Deluxe", "Rugged", "Compact", "Premium"]
NOUN = ["Backpack", "Water Bottle", "Headphones", "Desk Lamp", "Keyboard", "Mouse", "Sneakers", "T-Shirt",
        "Coffee Mug", "Notebook", "Charger", "Speaker", "Sunglasses", "Wallet", "Umbrella", "Jacket"]
BATCH = 2000


def rand_dt(days_back=365):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    return now - timedelta(seconds=random.randint(0, days_back * 86400))


def chunks(rows):
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def next_id(conn, table):
    return (conn.execute(select(func.max(table.c.id))).scalar() or 0) + 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customers", type=int, default=5000)
    ap.add_argument("--products", type=int, default=500)
    ap.add_argument("--orders", type=int, default=20000)
    ap.add_argument("--reset", action="store_true", help="delete all existing rows first")
    args = ap.parse_args()

    engine = db.make_engine("DB_")
    if engine.shop_config["engine"] != "mysql":
        raise SystemExit("seed.py writes explicit ids and is meant for the MySQL source only.")
    t0 = time.time()

    with engine.begin() as conn:
        if args.reset:
            for t in ["order_items", "orders", "products", "customers", "heartbeat"]:
                conn.execute(text(f"DELETE FROM {t}"))
                conn.execute(text(f"ALTER TABLE {t} AUTO_INCREMENT = 1"))
            print("Existing data deleted.")

        start = next_id(conn, db.customers)
        rows = []
        for i in range(start, start + args.customers):
            fn, ln = random.choice(FIRST), random.choice(LAST)
            created = rand_dt()
            rows.append({"id": i, "email": f"{fn}.{ln}.{i}@example.com".lower(), "full_name": f"{fn} {ln}",
                         "city": random.choice(CITIES), "created_at": created, "updated_at": created})
        for part in chunks(rows):
            conn.execute(insert(db.customers), part)
        customer_ids = list(conn.execute(select(db.customers.c.id)).scalars())
        print(f"customers: +{len(rows):,}")

        start = next_id(conn, db.products)
        rows = []
        for i in range(start, start + args.products):
            price = Decimal(random.randint(299, 49999)) / 100
            created = rand_dt()
            rows.append({"id": i, "sku": f"SKU-{i:06d}", "name": f"{random.choice(ADJ)} {random.choice(NOUN)}",
                         "description": f"Seeded product {i}. " + "Lorem ipsum dolor sit amet. " * random.randint(1, 6),
                         "price": price, "stock": random.randint(0, 500), "created_at": created, "updated_at": created})
        for part in chunks(rows):
            conn.execute(insert(db.products), part)
        print(f"products:  +{len(rows):,}")
        prices = dict(conn.execute(select(db.products.c.id, db.products.c.price)).all())
        product_ids = list(prices)

    if not customer_ids or not product_ids:
        raise SystemExit("Need at least one customer and one product to create orders.")

    statuses = ["pending", "paid", "shipped", "delivered", "delivered", "delivered", "cancelled"]
    with engine.begin() as conn:
        oid = next_id(conn, db.orders)
        iid = next_id(conn, db.order_items)
        order_rows, item_rows = [], []
        for n in range(args.orders):
            created = rand_dt()
            total = Decimal("0.00")
            for _ in range(random.randint(1, 4)):
                pid, qty = random.choice(product_ids), random.randint(1, 5)
                item_rows.append({"id": iid, "order_id": oid, "product_id": pid, "quantity": qty,
                                  "unit_price": prices[pid]})
                total += prices[pid] * qty
                iid += 1
            order_rows.append({"id": oid, "customer_id": random.choice(customer_ids), "status": random.choice(statuses),
                               "total": total, "created_at": created, "updated_at": created})
            oid += 1
            if len(order_rows) >= BATCH:
                conn.execute(insert(db.orders), order_rows)
                conn.execute(insert(db.order_items), item_rows)
                order_rows, item_rows = [], []
                print(f"  orders {n + 1:,}/{args.orders:,}", end="\r", flush=True)
        if order_rows:
            conn.execute(insert(db.orders), order_rows)
            conn.execute(insert(db.order_items), item_rows)
        print(f"orders:    +{args.orders:,}          ")

    with engine.connect() as conn:
        for t in db.TABLES:
            print(f"  {t:<12} {conn.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar():>10,} rows")
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
