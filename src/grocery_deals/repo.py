"""Write helpers shared by the deal sources and receipt importers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from .normalize import normalize_text


@dataclass
class DealIn:
    store: str
    raw_text: str
    sale_price: float
    brand: str | None = None
    name: str | None = None
    size_text: str | None = None
    category: str | None = None
    regular_price: float | None = None
    price_unit: str | None = None
    deal_limit: int | None = None
    coupon_required: bool = False
    upc: str | None = None
    sku: str | None = None
    source: str = "manual"


@dataclass
class PurchaseLine:
    raw_name: str
    paid_price: float
    qty: float = 1.0
    unit_price: float | None = None
    regular_unit_price: float | None = None
    brand: str | None = None
    name: str | None = None
    size_qty: float | None = None
    size_unit: str | None = None
    category: str | None = None
    upc: str | None = None
    sku: str | None = None
    shelf_stable: bool = False


@dataclass
class ReceiptIn:
    store: str
    date: str
    source: str
    source_ref: str
    lines: list[PurchaseLine] = field(default_factory=list)


def item_key(upc: str | None, sku: str | None, text: str) -> str:
    if upc:
        return f"upc:{upc.lstrip('0')}"
    if sku:
        return f"sku:{sku}"
    return f"txt:{normalize_text(text)}"


def save_deals(conn: sqlite3.Connection, deals: list[DealIn], week: date, seen_on: date | None = None) -> int:
    """Insert deals for an ad week (idempotent) and record price observations."""
    seen_on = seen_on or date.today()
    n = 0
    for d in deals:
        if d.sale_price is None or d.sale_price <= 0:
            continue
        cur = conn.execute(
            """INSERT INTO deal (store, week_start, raw_text, brand, name, size_text, category,
                   sale_price, regular_price, price_unit, deal_limit, coupon_required, upc, sku, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (store, week_start, raw_text) DO UPDATE SET
                   sale_price=excluded.sale_price, regular_price=excluded.regular_price,
                   coupon_required=excluded.coupon_required, deal_limit=excluded.deal_limit""",
            (d.store, week.isoformat(), d.raw_text, d.brand, d.name, d.size_text, d.category,
             round(d.sale_price, 2), d.regular_price, d.price_unit, d.deal_limit,
             int(d.coupon_required), d.upc, d.sku, d.source),
        )
        n += cur.rowcount
        record_price(conn, d.store, item_key(d.upc, d.sku, d.raw_text), seen_on,
                     d.sale_price, d.regular_price, d.source)
    conn.commit()
    return n


def record_price(conn, store, key, on: date | str, price, regular_price, source, product_id=None):
    if price is None or price <= 0:
        return
    conn.execute(
        """INSERT INTO price_obs (store, item_key, product_id, date, price, regular_price, source)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (store, item_key, date, source) DO UPDATE SET
               price=excluded.price, regular_price=excluded.regular_price,
               product_id=COALESCE(excluded.product_id, price_obs.product_id)""",
        (store, key, product_id, str(on), round(price, 2), regular_price, source),
    )


def create_product(conn, line: PurchaseLine) -> int:
    upc = line.upc.lstrip("0") if line.upc else None
    if upc:
        row = conn.execute("SELECT product_id FROM product WHERE upc=?", (upc,)).fetchone()
        if row:
            return row["product_id"]
    cur = conn.execute(
        """INSERT INTO product (name, brand, size_qty, size_unit, category, upc, shelf_stable)
           VALUES (?,?,?,?,?,?,?)""",
        ((line.name or line.raw_name).strip(), line.brand, line.size_qty, line.size_unit,
         line.category, upc, int(line.shelf_stable)),
    )
    return cur.lastrowid


def save_receipt(conn: sqlite3.Connection, receipt: ReceiptIn) -> int:
    """Insert purchase lines, linking each to a store_item and (via matching) a product.

    Returns the number of new purchase rows. Re-importing the same receipt is a no-op.
    """
    from .matching import match_purchase_line

    new = 0
    for i, line in enumerate(receipt.lines):
        exists = conn.execute("SELECT 1 FROM purchase WHERE source_ref=? AND line_no=?",
                              (receipt.source_ref, i)).fetchone()
        if exists:
            continue
        row = conn.execute("SELECT * FROM store_item WHERE store=? AND raw_name=?",
                           (receipt.store, line.raw_name)).fetchone()
        if row:
            store_item_id, product_id = row["store_item_id"], row["product_id"]
        else:
            product_id, method, conf = match_purchase_line(conn, line)
            if product_id is None:
                product_id, method, conf = create_product(conn, line), "new", 1.0
            cur = conn.execute(
                """INSERT INTO store_item (store, raw_name, sku, upc, product_id, match_method, match_confidence)
                   VALUES (?,?,?,?,?,?,?)""",
                (receipt.store, line.raw_name, line.sku, line.upc, product_id, method, conf),
            )
            store_item_id = cur.lastrowid
        qty = line.qty or 1.0
        unit_price = line.unit_price if line.unit_price is not None else (line.paid_price / qty if qty else None)
        conn.execute(
            """INSERT INTO purchase (date, store, store_item_id, qty, unit_price, paid_price,
                   regular_unit_price, source, source_ref, line_no)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (receipt.date, receipt.store, store_item_id, qty, unit_price, line.paid_price,
             line.regular_unit_price, receipt.source, receipt.source_ref, i),
        )
        if unit_price:
            record_price(conn, receipt.store, item_key(line.upc, line.sku, line.raw_name), receipt.date,
                         unit_price, line.regular_unit_price, "receipt", product_id)
        new += 1
    conn.commit()
    return new
