"""Manual / fallback deal import from CSV.

Columns (header names are case-insensitive; only store, item and sale_price are required):
store, item, sale_price, regular_price, brand, name, size, category, unit, limit, coupon, upc
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from ..repo import DealIn, save_deals


def _f(v):
    try:
        return float(str(v).replace("$", "").strip()) if v not in (None, "") else None
    except ValueError:
        return None


def read_deals(path: str | Path, default_store: str | None = None) -> list[DealIn]:
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            store = r.get("store") or default_store
            text = r.get("item") or r.get("raw_text") or r.get("description")
            price = _f(r.get("sale_price") or r.get("price"))
            if not (store and text and price):
                continue
            out.append(DealIn(
                store=store, raw_text=text, sale_price=price, regular_price=_f(r.get("regular_price")),
                brand=r.get("brand") or None, name=r.get("name") or None, size_text=r.get("size") or None,
                category=r.get("category") or None, price_unit=r.get("unit") or None,
                deal_limit=int(_f(r.get("limit"))) if _f(r.get("limit")) else None,
                coupon_required=r.get("coupon", "").lower() in ("1", "y", "yes", "true"),
                upc=r.get("upc") or None, source="csv",
            ))
    return out


def ingest(conn, path: str | Path, week: date, default_store: str | None = None) -> int:
    return save_deals(conn, read_deals(path, default_store), week)
