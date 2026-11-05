"""Seed a database with a realistic household's history and one week of ads.

`grocery demo` uses it to show the full pipeline without any credentials.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .repo import DealIn, PurchaseLine, ReceiptIn, save_deals, save_receipt

# (raw receipt name, brand, name, size_qty, size_unit, category, every_n_days, price, store, shelf_stable)
HOUSEHOLD = [
    ("TLMK CHED SHRP 8OZ", "Tillamook", "sharp cheddar cheese", 8, "oz", "dairy", 14, 4.99, "fred_meyer", False),
    ("STARBUCKS PIKE PL 12OZ", "Starbucks", "pike place ground coffee", 12, "oz", "coffee", 21, 10.99, "safeway", True),
    ("FAGE TOTAL 0% 32OZ", "Fage", "total 0% greek yogurt", 32, "oz", "dairy", 10, 6.49, "qfc", False),
    ("ORG BANANAS", None, "organic bananas", None, None, "produce", 7, 0.79, "fred_meyer", False),
    ("DAVES KLR BRD 21OZ", "Dave's Killer Bread", "21 whole grains bread", 27, "oz", "bakery", 9, 6.29, "safeway", False),
    ("ORG VALLEY WHL MLK GAL", "Organic Valley", "whole milk", 1, "gal", "dairy", 8, 6.99, "qfc", False),
    ("KS OLIVE OIL 2L", "Kirkland", "olive oil", 2, "l", "pantry", 60, 18.99, "safeway", True),
    ("BNLS SKNLS CHKN BRST", None, "boneless skinless chicken breast", None, None, "meat", 12, 4.99, "fred_meyer", False),
    ("RAO HMD MARINARA 24OZ", "Rao's", "homemade marinara sauce", 24, "oz", "pantry", 20, 8.49, "metmarket", True),
    ("BEN JERRY HALF BAKED", "Ben & Jerry's", "half baked ice cream", 16, "oz", "frozen", 30, 5.99, "qfc", True),
    ("TILLAMOOK BUTTER 1LB", "Tillamook", "salted butter", 1, "lb", "dairy", 25, 6.49, "fred_meyer", False),
    ("LACROIX LIME 8PK", "LaCroix", "lime sparkling water", 8, "ct", "beverages", 12, 4.99, "safeway", True),
]

AD = {
    "fred_meyer": [
        DealIn("fred_meyer", "Tillamook Cheese 7-8 oz, select varieties", 3.49, "Tillamook", "cheese", "7-8 oz",
               "dairy", 4.99, "each", coupon_required=True, source="demo"),
        DealIn("fred_meyer", "Boneless Skinless Chicken Breast", 2.99, None, "boneless skinless chicken breast",
               None, "meat", 4.99, "lb", source="demo"),
        DealIn("fred_meyer", "Tillamook Butter 1 lb", 4.99, "Tillamook", "butter", "1 lb", "dairy", 6.49, source="demo"),
        DealIn("fred_meyer", "Simple Truth Organic Spinach 5 oz", 2.50, "Simple Truth", "organic spinach", "5 oz",
               "produce", 3.99, source="demo"),
    ],
    "qfc": [
        DealIn("qfc", "Fage Total Greek Yogurt 32 oz", 4.99, "Fage", "total greek yogurt", "32 oz", "dairy", 6.49,
               source="demo"),
        DealIn("qfc", "Chobani Greek Yogurt 32 oz", 4.49, "Chobani", "greek yogurt", "32 oz", "dairy", 6.29,
               source="demo"),
        DealIn("qfc", "Ben & Jerry's Ice Cream Pint", 3.99, "Ben & Jerry's", "ice cream", "16 oz", "frozen", 5.99,
               deal_limit=4, source="demo"),
    ],
    "safeway": [
        DealIn("safeway", "Starbucks Ground Coffee 10-12 oz", 7.99, "Starbucks", "ground coffee", "10-12 oz",
               "coffee", 10.99, coupon_required=True, source="demo"),
        DealIn("safeway", "LaCroix Sparkling Water 8 pk", 3.33, "LaCroix", "sparkling water", "8 ct", "beverages",
               4.99, source="demo"),
        DealIn("safeway", "Dave's Killer Bread 20.5-27 oz", 4.99, "Dave's Killer Bread", "bread", "20.5-27 oz",
               "bakery", 6.29, source="demo"),
        DealIn("safeway", "Lucerne Shredded Cheese 8 oz", 2.49, "Lucerne", "shredded cheese", "8 oz", "dairy",
               4.29, source="demo"),
    ],
    "metmarket": [
        DealIn("metmarket", "Rao's Homemade Sauce 24 oz", 6.99, "Rao's", "homemade sauce", "24 oz", "pantry",
               8.49, source="demo"),
        DealIn("metmarket", "Metropolitan Market Cookies 6 ct", 5.99, None, "cookies", "6 ct", "bakery", 7.99,
               source="demo"),
    ],
}


def seed(conn, today: date | None = None, week: date | None = None, seed_value: int = 7) -> None:
    from .normalize import week_start

    rng = random.Random(seed_value)
    today = today or date.today()
    week = week or week_start(today)
    start = today - timedelta(days=365)
    receipts: dict[tuple[str, str], ReceiptIn] = {}
    for raw, brand, name, sq, su, cat, every, price, store, stable in HOUSEHOLD:
        d = start + timedelta(days=rng.randint(0, every))
        while d < today - timedelta(days=1):
            qty = 1.0 if su else round(rng.uniform(1.5, 3.0), 2)  # weighed/loose items
            on_sale = rng.random() < 0.2
            unit = round(price * (0.8 if on_sale else 1.0) * rng.uniform(0.97, 1.05), 2)
            key = (store, d.isoformat())
            rec = receipts.setdefault(key, ReceiptIn(store, d.isoformat(), "demo", f"demo:{store}:{d}"))
            rec.lines.append(PurchaseLine(
                raw_name=raw, paid_price=round(unit * qty, 2), qty=qty, unit_price=unit,
                regular_unit_price=round(price, 2) if on_sale else None, brand=brand, name=name,
                size_qty=sq, size_unit=su, category=cat, shelf_stable=stable))
            d += timedelta(days=max(2, int(rng.gauss(every, every * 0.2))))
    for rec in sorted(receipts.values(), key=lambda r: r.date):
        save_receipt(conn, rec)
    for store, deals in AD.items():
        save_deals(conn, deals, week, today)
