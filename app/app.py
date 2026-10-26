"""ShopLite: a small order-management site used to practise an on-prem -> AWS migration."""
import functools
import os
import socket
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)
from sqlalchemy import insert, text, update
from sqlalchemy.exc import SQLAlchemyError

import db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
engine = db.make_engine("DB_")
ENGINE_NAME = engine.shop_config["engine"]
PAGE_SIZE = 50


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _instance_identity():
    """EC2 instance id via IMDSv2, or None when not on EC2 (i.e. still on-prem)."""
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(req, timeout=0.5).read().decode()
        req = urllib.request.Request("http://169.254.169.254/latest/meta-data/instance-id",
                                     headers={"X-aws-ec2-metadata-token": token})
        return urllib.request.urlopen(req, timeout=0.5).read().decode()
    except Exception:
        return None


HOSTNAME = socket.gethostname()
INSTANCE_ID = _instance_identity()


def app_read_only():
    return os.environ.get("APP_READ_ONLY", "false").lower() in ("1", "true", "yes", "on")


@app.context_processor
def inject_runtime():
    db_read_only = None
    try:
        with engine.connect() as conn:
            db_read_only = db.is_db_read_only(conn, ENGINE_NAME)
    except SQLAlchemyError:
        pass
    return {
        "runtime": {
            "hostname": HOSTNAME,
            "instance_id": INSTANCE_ID,
            "engine": "PostgreSQL" if ENGINE_NAME == "postgres" else "MySQL",
            "db_host": engine.shop_config["host"],
            "db_read_only": db_read_only,
            "app_read_only": app_read_only(),
        },
        "statuses": db.ORDER_STATUSES,
    }


def write_action(view):
    """Wrap a POST handler: refuse in app read-only mode and turn DB read-only errors into a flash."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        back = request.referrer or url_for("dashboard")
        if app_read_only():
            flash("Maintenance mode: the site is read-only while the database is migrated.", "warn")
            return redirect(back)
        try:
            return view(*args, **kwargs)
        except SQLAlchemyError as exc:
            if db.is_read_only_error(exc):
                flash("The database is in read-only mode (migration cutover in progress). "
                      "Your change was not saved.", "warn")
            else:
                app.logger.exception("write failed")
                flash(f"Database error: {getattr(exc, 'orig', exc)}", "error")
            return redirect(back)
    return wrapper


# ----------------------------------------------------------------- pages

@app.route("/")
def dashboard():
    with engine.connect() as conn:
        counts = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in db.TABLES}
        by_status = dict(conn.execute(text(
            "SELECT status, COUNT(*) FROM orders GROUP BY status")).all())
        revenue = conn.execute(text(
            "SELECT COALESCE(SUM(total), 0) FROM orders WHERE status <> 'cancelled'")).scalar()
        recent = conn.execute(text(
            "SELECT o.id, o.status, o.total, o.created_at, c.full_name "
            "FROM orders o JOIN customers c ON c.id = o.customer_id "
            "ORDER BY o.id DESC LIMIT 10")).mappings().all()
        last_hb = conn.execute(text(
            "SELECT source_host, written_at FROM heartbeat ORDER BY id DESC LIMIT 1")).mappings().first()
    # The view is created by SCT on the target; if its conversion failed the rest of the page still works.
    top_customers, view_error = [], None
    try:
        with engine.connect() as conn:
            top_customers = conn.execute(text(
                "SELECT customer_id, full_name, city, order_count, revenue "
                "FROM v_customer_revenue ORDER BY revenue DESC LIMIT 5")).mappings().all()
    except SQLAlchemyError as exc:
        view_error = str(getattr(exc, "orig", exc)).splitlines()[0]
    return render_template("dashboard.html", counts=counts, by_status=by_status, revenue=revenue,
                           recent=recent, last_hb=last_hb, top_customers=top_customers,
                           view_error=view_error)


@app.route("/products")
def product_list():
    page = max(request.args.get("page", 1, type=int), 1)
    q = request.args.get("q", "").strip()
    where, params = "", {"lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}
    if q:
        where, params["q"] = "WHERE LOWER(name) LIKE :q OR LOWER(sku) LIKE :q", f"%{q.lower()}%"
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT id, sku, name, price, stock, updated_at FROM products {where} "
            "ORDER BY id DESC LIMIT :lim OFFSET :off"), params).mappings().all()
    return render_template("products.html", rows=rows, page=page, q=q, page_size=PAGE_SIZE)


@app.route("/products", methods=["POST"])
@write_action
def product_create():
    f = request.form
    now = utcnow()
    with engine.begin() as conn:
        pid = conn.execute(insert(db.products).values(
            sku=f["sku"].strip(), name=f["name"].strip(), description=f.get("description", "").strip(),
            price=Decimal(f["price"]), stock=int(f.get("stock") or 0),
            created_at=now, updated_at=now)).inserted_primary_key[0]
    flash(f"Product #{pid} created.", "ok")
    return redirect(url_for("product_list"))


@app.route("/products/<int:pid>/restock", methods=["POST"])
@write_action
def product_restock(pid):
    qty = request.form.get("qty", 10, type=int)
    with engine.begin() as conn:
        conn.execute(update(db.products).where(db.products.c.id == pid).values(
            stock=db.products.c.stock + qty, updated_at=utcnow()))
    flash(f"Product #{pid} restocked (+{qty}).", "ok")
    return redirect(request.referrer or url_for("product_list"))


@app.route("/customers")
def customer_list():
    page = max(request.args.get("page", 1, type=int), 1)
    q = request.args.get("q", "").strip()
    where, params = "", {"lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}
    if q:
        where, params["q"] = "WHERE LOWER(full_name) LIKE :q OR LOWER(email) LIKE :q OR LOWER(city) LIKE :q", f"%{q.lower()}%"
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT id, email, full_name, city, created_at FROM customers {where} "
            "ORDER BY id DESC LIMIT :lim OFFSET :off"), params).mappings().all()
    return render_template("customers.html", rows=rows, page=page, q=q, page_size=PAGE_SIZE)


@app.route("/customers", methods=["POST"])
@write_action
def customer_create():
    f = request.form
    now = utcnow()
    with engine.begin() as conn:
        cid = conn.execute(insert(db.customers).values(
            email=f["email"].strip().lower(), full_name=f["full_name"].strip(),
            city=f.get("city", "").strip() or None, created_at=now, updated_at=now
        )).inserted_primary_key[0]
    flash(f"Customer #{cid} created.", "ok")
    return redirect(url_for("customer_list"))


@app.route("/orders")
def order_list():
    page = max(request.args.get("page", 1, type=int), 1)
    status = request.args.get("status", "")
    where, params = "", {"lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE}
    if status in db.ORDER_STATUSES:
        where, params["status"] = "WHERE o.status = :status", status
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT o.id, o.status, o.total, o.created_at, o.updated_at, c.full_name "
            f"FROM orders o JOIN customers c ON c.id = o.customer_id {where} "
            "ORDER BY o.id DESC LIMIT :lim OFFSET :off"), params).mappings().all()
    return render_template("orders.html", rows=rows, page=page, status=status, page_size=PAGE_SIZE)


@app.route("/orders/new")
def order_new():
    with engine.connect() as conn:
        custs = conn.execute(text(
            "SELECT id, full_name, email FROM customers ORDER BY id DESC LIMIT 200")).mappings().all()
        prods = conn.execute(text(
            "SELECT id, sku, name, price, stock FROM products WHERE stock > 0 "
            "ORDER BY id DESC LIMIT 200")).mappings().all()
    return render_template("order_new.html", customers=custs, products=prods)


@app.route("/orders", methods=["POST"])
@write_action
def order_create():
    customer_id = request.form.get("customer_id", type=int)
    lines = []
    for key, val in request.form.items():
        if key.startswith("qty_") and val.strip() and int(val) > 0:
            lines.append((int(key[4:]), int(val)))
    if not customer_id or not lines:
        flash("Pick a customer and at least one product quantity.", "error")
        return redirect(url_for("order_new"))
    order_id = place_order(customer_id, lines)
    flash(f"Order #{order_id} placed.", "ok")
    return redirect(url_for("order_detail", oid=order_id))


def place_order(customer_id, lines, status="pending"):
    """Create an order with its items in one transaction. lines = [(product_id, qty), ...]."""
    now = utcnow()
    with engine.begin() as conn:
        ids = [pid for pid, _ in lines]
        prices = dict(conn.execute(
            db.products.select().with_only_columns(db.products.c.id, db.products.c.price)
            .where(db.products.c.id.in_(ids))).all())
        total = sum(prices[pid] * qty for pid, qty in lines if pid in prices)
        oid = conn.execute(insert(db.orders).values(
            customer_id=customer_id, status=status, total=total, created_at=now, updated_at=now
        )).inserted_primary_key[0]
        for pid, qty in lines:
            if pid not in prices:
                continue
            conn.execute(insert(db.order_items).values(
                order_id=oid, product_id=pid, quantity=qty, unit_price=prices[pid]))
            conn.execute(update(db.products).where(db.products.c.id == pid).values(
                stock=db.products.c.stock - qty, updated_at=now))
    return oid


@app.route("/orders/<int:oid>")
def order_detail(oid):
    with engine.connect() as conn:
        order = conn.execute(text(
            "SELECT o.*, c.full_name, c.email FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.id = :id"), {"id": oid}).mappings().first()
        if not order:
            abort(404)
        items = conn.execute(text(
            "SELECT i.quantity, i.unit_price, p.sku, p.name FROM order_items i "
            "JOIN products p ON p.id = i.product_id WHERE i.order_id = :id ORDER BY i.id"),
            {"id": oid}).mappings().all()
    return render_template("order_detail.html", order=order, items=items)


@app.route("/orders/<int:oid>/status", methods=["POST"])
@write_action
def order_set_status(oid):
    status = request.form["status"]
    if status not in db.ORDER_STATUSES:
        abort(400)
    with engine.begin() as conn:
        conn.execute(update(db.orders).where(db.orders.c.id == oid).values(
            status=status, updated_at=utcnow()))
    flash(f"Order #{oid} is now {status}.", "ok")
    return redirect(url_for("order_detail", oid=oid))


# ----------------------------------------------------------------- health / API

@app.route("/health")
def health():
    """ALB target-group health check. 200 only when the database answers."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return jsonify(status="ok", db=ENGINE_NAME, host=HOSTNAME), 200
    except SQLAlchemyError as exc:
        return jsonify(status="db_unreachable", error=str(getattr(exc, "orig", exc))), 503


@app.route("/api/stats")
def api_stats():
    with engine.connect() as conn:
        counts = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in db.TABLES}
        read_only = db.is_db_read_only(conn, ENGINE_NAME)
    return jsonify(engine=ENGINE_NAME, db_host=engine.shop_config["host"], web_host=HOSTNAME,
                   instance_id=INSTANCE_ID, db_read_only=read_only,
                   app_read_only=app_read_only(), counts=counts)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
