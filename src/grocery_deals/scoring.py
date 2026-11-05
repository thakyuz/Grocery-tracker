"""Rank this week's deals for you.

score = frequency x discount vs typical x due-soon factor

Tier 1: deals matched to a product you buy.
Tier 2: category-level matches ("you buy Greek yogurt, Fage is on sale").
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class RankedDeal:
    deal_id: int
    store: str
    raw_text: str
    sale_price: float
    price_unit: str | None
    coupon_required: bool
    deal_limit: int | None
    product_id: int | None
    product_name: str | None
    tier: int
    score: float
    discount: float          # 0..1 vs your typical price (or regular price)
    reference_price: float | None
    est_saving: float | None  # per typical purchase
    is_90d_low: bool
    advice: str
    match_method: str | None = None
    match_confidence: float | None = None


def frequency_factor(purchases_6mo: int) -> float:
    return min(1.0, math.log1p(purchases_6mo) / math.log1p(12))


def due_factor(days_since: int | None, avg_gap: float | None, shelf_stable: bool) -> tuple[float, str]:
    if days_since is None or not avg_gap:
        return 0.7, "buy if you need it"
    ratio = days_since / avg_gap
    factor = max(0.3, min(1.2, 0.3 + 0.7 * ratio))
    if ratio >= 0.8:
        advice = "due now - buy"
    elif shelf_stable:
        factor = max(factor, 0.8)
        advice = "keeps well - stock up"
    else:
        advice = f"not due for ~{max(1, round(avg_gap - days_since))} days"
    return round(factor, 3), advice


def _ninety_day_low(conn, product_id: int, before: str) -> float | None:
    start = (date.fromisoformat(before) - timedelta(days=90)).isoformat()
    row = conn.execute(
        "SELECT MIN(price) AS low FROM price_obs WHERE product_id=? AND date>=? AND date<?",
        (product_id, start, before)).fetchone()
    return row["low"] if row else None


def rank_deals(conn: sqlite3.Connection, week: str, today: date | None = None,
               tier2_limit: int = 10) -> list[RankedDeal]:
    today = today or date.today()
    ranked: list[RankedDeal] = []

    rows = conn.execute(
        """SELECT d.*, p.name AS product_name, p.brand AS product_brand, p.shelf_stable,
                  c.purchases_6mo, c.avg_days_between, c.last_bought, c.typical_qty, c.typical_price
           FROM deal d JOIN product p ON p.product_id = d.product_id
           LEFT JOIN product_cadence c ON c.product_id = d.product_id
           WHERE d.week_start = ?""", (week,)).fetchall()

    best_per_product: dict[int, RankedDeal] = {}
    for r in rows:
        ref = r["typical_price"] or r["regular_price"]
        if not ref:
            continue
        discount = (ref - r["sale_price"]) / ref
        if discount <= 0.02:
            continue  # not cheaper than what you usually pay: not a real sale for you
        low = _ninety_day_low(conn, r["product_id"], week)
        is_low = low is None or r["sale_price"] <= low
        days_since = (today - date.fromisoformat(r["last_bought"])).days if r["last_bought"] else None
        due, advice = due_factor(days_since, r["avg_days_between"], bool(r["shelf_stable"]))
        score = frequency_factor(r["purchases_6mo"] or 0) * min(discount, 0.8) * due
        if is_low and low is not None:
            score *= 1.1
        name = f"{r['product_brand']} {r['product_name']}" if r["product_brand"] else r["product_name"]
        rd = RankedDeal(
            deal_id=r["deal_id"], store=r["store"], raw_text=r["raw_text"], sale_price=r["sale_price"],
            price_unit=r["price_unit"], coupon_required=bool(r["coupon_required"]), deal_limit=r["deal_limit"],
            product_id=r["product_id"], product_name=name, tier=1, score=round(score, 4),
            discount=round(discount, 3), reference_price=ref,
            est_saving=round((ref - r["sale_price"]) * (r["typical_qty"] or 1), 2),
            is_90d_low=is_low and low is not None, advice=advice,
            match_method=r["match_method"], match_confidence=r["match_confidence"],
        )
        prev = best_per_product.get(r["product_id"])
        if prev is None or rd.score > prev.score:
            best_per_product[r["product_id"]] = rd
    ranked.extend(sorted(best_per_product.values(), key=lambda d: d.score, reverse=True))

    # Tier 2: unmatched deals in categories you buy often, by discount.
    cats = conn.execute(
        """SELECT LOWER(p.category) AS cat, SUM(c.purchases_6mo) AS n
           FROM product p JOIN product_cadence c USING (product_id)
           WHERE p.category IS NOT NULL GROUP BY 1 HAVING n >= 2""").fetchall()
    cat_weight = {c["cat"]: frequency_factor(c["n"]) for c in cats}
    tier2 = []
    for r in conn.execute(
            """SELECT d.*, p.name AS maybe_name, p.brand AS maybe_brand
               FROM deal d
               LEFT JOIN match_review mr ON mr.deal_id = d.deal_id AND mr.status = 'pending'
               LEFT JOIN product p ON p.product_id = mr.product_id
               WHERE d.week_start=? AND d.product_id IS NULL
               AND d.regular_price IS NOT NULL AND d.regular_price > d.sale_price""", (week,)):
        discount = (r["regular_price"] - r["sale_price"]) / r["regular_price"]
        if r["maybe_name"]:
            # Possible match waiting for review: show it, flagged, near the top of tier 2.
            maybe = f"{r['maybe_brand']} {r['maybe_name']}" if r["maybe_brand"] else r["maybe_name"]
            w, advice = 1.0, f"maybe your {maybe}? confirm with `grocery review`"
        else:
            w = cat_weight.get((r["category"] or "").lower())
            if not w or discount < 0.15:
                continue
            advice = f"you buy {r['category'].lower()}"
        tier2.append(RankedDeal(
            deal_id=r["deal_id"], store=r["store"], raw_text=r["raw_text"], sale_price=r["sale_price"],
            price_unit=r["price_unit"], coupon_required=bool(r["coupon_required"]),
            deal_limit=r["deal_limit"], product_id=None, product_name=None, tier=2,
            score=round(w * min(discount, 0.8) * 0.5, 4), discount=round(discount, 3),
            reference_price=r["regular_price"], est_saving=None, is_90d_low=False, advice=advice,
        ))
    ranked.extend(sorted(tier2, key=lambda d: d.score, reverse=True)[:tier2_limit])
    return ranked
