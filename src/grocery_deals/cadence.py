"""Buying cadence per product: how often you buy it, how much, at what price."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from statistics import mean, median


def rebuild_cadence(conn: sqlite3.Connection, today: date | None = None) -> int:
    today = today or date.today()
    six_months_ago = (today - timedelta(days=182)).isoformat()
    rows = conn.execute(
        """SELECT si.product_id, p.date, p.qty, p.unit_price
           FROM purchase p JOIN store_item si USING (store_item_id)
           WHERE si.product_id IS NOT NULL AND p.date <= ?
           ORDER BY si.product_id, p.date""", (today.isoformat(),)).fetchall()

    by_product: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_product.setdefault(r["product_id"], []).append(r)

    conn.execute("DELETE FROM product_cadence")
    for pid, items in by_product.items():
        days = sorted({r["date"] for r in items})
        gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:])]
        gaps = [g for g in gaps if g > 0]
        qty_per_day: dict[str, float] = {}
        for r in items:
            qty_per_day[r["date"]] = qty_per_day.get(r["date"], 0) + (r["qty"] or 1)
        prices = [r["unit_price"] for r in items if r["unit_price"]]
        conn.execute(
            """INSERT INTO product_cadence (product_id, purchase_days, purchases_6mo, avg_days_between,
                   last_bought, typical_qty, typical_price) VALUES (?,?,?,?,?,?,?)""",
            (pid, len(days), sum(1 for d in days if d >= six_months_ago),
             round(mean(gaps), 1) if gaps else None, days[-1],
             median(qty_per_day.values()), round(median(prices), 2) if prices else None),
        )
    conn.commit()
    return len(by_product)


def top_products(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Your most frequently bought products (the matching test set)."""
    return conn.execute(
        """SELECT p.*, c.purchase_days, c.purchases_6mo, c.avg_days_between, c.last_bought,
                  c.typical_price, c.typical_qty
           FROM product_cadence c JOIN product p USING (product_id)
           ORDER BY c.purchases_6mo DESC, c.purchase_days DESC LIMIT ?""", (limit,)).fetchall()
