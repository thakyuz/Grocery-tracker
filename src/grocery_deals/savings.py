"""Savings tracker: what you saved per month."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class MonthSavings:
    month: str
    spent: float
    receipt_savings: float   # card/sale savings printed on receipts
    digest_hits: int         # digest items you then bought that week, at that store
    digest_savings: float    # estimated savings on those hits


def monthly_savings(conn: sqlite3.Connection, months: int = 6) -> list[MonthSavings]:
    base = conn.execute(
        """SELECT substr(date, 1, 7) AS month,
                  ROUND(SUM(COALESCE(paid_price, unit_price * qty)), 2) AS spent,
                  ROUND(SUM(CASE WHEN regular_unit_price > unit_price
                                 THEN (regular_unit_price - unit_price) * qty ELSE 0 END), 2) AS saved
           FROM purchase GROUP BY 1 ORDER BY 1 DESC LIMIT ?""", (months,)).fetchall()
    hits = conn.execute(
        """SELECT month, COUNT(*) AS n, ROUND(SUM(est), 2) AS est FROM (
               SELECT di.deal_id, substr(MIN(p.date), 1, 7) AS month, MAX(di.est_saving) AS est
               FROM digest_item di
               JOIN deal d ON d.deal_id = di.deal_id
               JOIN store_item si ON si.product_id = di.product_id AND si.store = d.store
               JOIN purchase p ON p.store_item_id = si.store_item_id
                    AND p.date >= di.week_start AND p.date <= date(di.week_start, '+6 days')
               WHERE di.tier = 1 GROUP BY di.week_start, di.deal_id)
           GROUP BY month""").fetchall()
    by_month = {h["month"]: h for h in hits}
    return [
        MonthSavings(
            month=b["month"], spent=b["spent"] or 0.0, receipt_savings=b["saved"] or 0.0,
            digest_hits=by_month[b["month"]]["n"] if b["month"] in by_month else 0,
            digest_savings=(by_month[b["month"]]["est"] or 0.0) if b["month"] in by_month else 0.0,
        )
        for b in base
    ]
