#!/usr/bin/env python3
"""Compare the MySQL source (SRC_*) with the RDS PostgreSQL target (TGT_*).

    python tools/verify_migration.py              # counts + aggregate checksums + heartbeat lag
    python tools/verify_migration.py --watch 5    # repeat every 5s while CDC catches up
    python tools/verify_migration.py --deep       # row-by-row comparison of every column

Exit code 0 = everything matches, 1 = differences found (handy in a cutover checklist).
"""
import argparse
import hashlib
import sys
import time
from datetime import datetime
from decimal import Decimal

from _common import db
from sqlalchemy import text

COLUMNS = {
    "customers":   ["id", "email", "full_name", "city", "is_active", "created_at", "updated_at"],
    "products":    ["id", "sku", "name", "description", "price", "stock", "created_at", "updated_at"],
    "orders":      ["id", "customer_id", "status", "total", "created_at", "updated_at"],
    "order_items": ["id", "order_id", "product_id", "quantity", "unit_price"],
    "heartbeat":   ["id", "source_host", "written_at"],
}
# Aggregates that are identical SQL on both engines.
AGGREGATES = {
    "customers":   "COUNT(*), MAX(id), SUM(id)",
    "products":    "COUNT(*), MAX(id), SUM(id), SUM(price), SUM(stock)",
    "orders":      "COUNT(*), MAX(id), SUM(id), SUM(total), SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END)",
    "order_items": "COUNT(*), MAX(id), SUM(id), SUM(quantity), SUM(unit_price * quantity)",
    "heartbeat":   "COUNT(*), MAX(id)",
}
AGG_LABELS = {
    "customers": ["rows", "max_id", "sum_id"],
    "products": ["rows", "max_id", "sum_id", "sum_price", "sum_stock"],
    "orders": ["rows", "max_id", "sum_id", "sum_total", "delivered"],
    "order_items": ["rows", "max_id", "sum_id", "sum_qty", "sum_value"],
    "heartbeat": ["rows", "max_id"],
}


def norm(v):
    """Make MySQL and PostgreSQL values comparable (tinyint vs boolean, Decimal scale, etc.)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ")
    return v


def aggregates(conn, table):
    row = conn.execute(text(f"SELECT {AGGREGATES[table]} FROM {table}")).one()
    return [norm(Decimal(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for v in row]


def heartbeat_info(conn):
    return conn.execute(text("SELECT id, written_at FROM heartbeat ORDER BY id DESC LIMIT 1")).first()


def quick_check(src, tgt):
    ok = True
    print(f"{'table':<12} {'metric':<10} {'source':>20} {'target':>20}")
    print("-" * 66)
    with src.connect() as s, tgt.connect() as t:
        for table in db.TABLES:
            try:
                a = aggregates(s, table)
                b = aggregates(t, table)
            except Exception as exc:
                print(f"{table:<12} ERROR: {str(getattr(exc, 'orig', exc)).splitlines()[0]}")
                ok = False
                continue
            for label, x, y in zip(AGG_LABELS[table], a, b):
                mark = "" if x == y else "   <-- DIFF"
                ok &= x == y
                print(f"{table:<12} {label:<10} {str(x):>20} {str(y):>20}{mark}")
            print()
        hs, ht = heartbeat_info(s), heartbeat_info(t)
    if hs:
        if ht:
            behind = hs.id - ht.id
            lag = (hs.written_at - ht.written_at).total_seconds()
            print(f"Heartbeat: source id={hs.id} ({hs.written_at})  target id={ht.id} ({ht.written_at})")
            print(f"           target is {behind} heartbeats / {lag:.1f}s behind the source"
                  + ("  -> IN SYNC" if behind == 0 else ""))
        else:
            print(f"Heartbeat: source id={hs.id}, target has no heartbeat rows yet")
    return ok


def row_digest(row):
    return hashlib.md5(repr([norm(v) for v in row]).encode()).hexdigest()


def deep_check(src, tgt, sample_diffs=5):
    ok = True
    with src.connect() as s, tgt.connect() as t:
        for table, cols in COLUMNS.items():
            sql = text(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id")
            a = {r[0]: r for r in s.execute(sql)}
            b = {r[0]: r for r in t.execute(sql)}
            missing = sorted(set(a) - set(b))
            extra = sorted(set(b) - set(a))
            changed = [k for k in a.keys() & b.keys() if row_digest(a[k]) != row_digest(b[k])]
            status = "OK" if not (missing or extra or changed) else "DIFF"
            ok &= status == "OK"
            print(f"{table:<12} {status:<4} source={len(a):>8,} target={len(b):>8,} "
                  f"missing_on_target={len(missing):,} extra_on_target={len(extra):,} changed={len(changed):,}")
            if missing:
                print(f"    missing ids: {missing[:10]}")
            if extra:
                print(f"    extra ids:   {extra[:10]}")
            for k in sorted(changed)[:sample_diffs]:
                diffs = [f"{c}: {norm(x)!r} != {norm(y)!r}" for c, x, y in zip(cols, a[k], b[k]) if norm(x) != norm(y)]
                print(f"    id={k}: " + "; ".join(diffs))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deep", action="store_true", help="compare every row and column")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="repeat the quick check")
    args = ap.parse_args()

    src, tgt = db.make_engine("SRC_"), db.make_engine("TGT_")
    for name, e in (("source", src), ("target", tgt)):
        c = e.shop_config
        print(f"{name}: {c['engine']} {c['host']}:{c['port']}/{c['name']}"
              + (f" schema={c['schema']}" if c["engine"] == "postgres" else ""))
    print()

    if args.watch:
        try:
            while True:
                print(f"===== {time.strftime('%H:%M:%S')} =====")
                quick_check(src, tgt)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return
    ok = deep_check(src, tgt) if args.deep else quick_check(src, tgt)
    print("\nRESULT:", "MATCH" if ok else "DIFFERENCES FOUND")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
