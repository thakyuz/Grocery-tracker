"""Kroger public developer API: Fred Meyer and QFC (one connector, two location IDs).

The Products API returns regular and promo price per location. There is no
"list all promos" endpoint, so we search for the products you actually buy
(plus any extra terms you configure) and keep the ones with a promo price.
Every price seen is stored in price_obs, sale or not.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import httpx

from ..repo import DealIn, item_key, record_price, save_deals

log = logging.getLogger(__name__)

API = "https://api.kroger.com/v1"


class KrogerClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.http.post(
            f"{API}/connect/oauth2/token",
            data={"grant_type": "client_credentials", "scope": "product.compact"},
            auth=(self.client_id, self.client_secret),
        )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 1800))
        return self._token

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(3):
            r = self.http.get(f"{API}{path}", params=params,
                              headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return r.json()

    def search_products(self, term: str, location_id: str, limit: int = 20) -> list[dict]:
        return self._get("/products", {
            "filter.term": term[:100], "filter.locationId": location_id, "filter.limit": limit,
        }).get("data", [])

    def locations(self, zip_code: str, limit: int = 20) -> list[dict]:
        return self._get("/locations", {"filter.zipCode.near": zip_code, "filter.limit": limit}).get("data", [])


def product_to_deal(p: dict, store: str) -> tuple[DealIn | None, float | None, float | None]:
    """Return (deal if on promo, current price, regular price)."""
    items = p.get("items") or [{}]
    item = items[0]
    price = item.get("price") or {}
    regular = price.get("regular") or None
    promo = price.get("promo") or None
    current = promo if promo and (not regular or promo < regular) else regular
    if not (promo and regular and 0 < promo < regular):
        return None, current, regular
    cats = p.get("categories") or []
    desc = p.get("description") or ""
    return DealIn(
        store=store, raw_text=f"{desc} {item.get('size') or ''}".strip(), sale_price=float(promo),
        brand=p.get("brand"), name=desc, size_text=item.get("size"),
        category=cats[0].lower() if cats else None, regular_price=float(regular),
        price_unit="lb" if item.get("soldBy", "").upper() == "WEIGHT" else "each",
        upc=(p.get("upc") or "").lstrip("0") or None, sku=p.get("productId"), source="kroger_api",
    ), current, regular


def search_terms(conn, extra_terms: list[str], limit: int = 60) -> list[str]:
    rows = conn.execute(
        """SELECT p.brand, p.name FROM product_cadence c JOIN product p USING (product_id)
           ORDER BY c.purchases_6mo DESC, c.purchase_days DESC LIMIT ?""", (limit,)).fetchall()
    terms = [f"{r['brand'] or ''} {r['name']}".strip() for r in rows]
    seen, out = set(), []
    for t in terms + extra_terms:
        k = t.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def ingest(conn, client: KrogerClient, locations: dict[str, str], week: date,
           extra_terms: list[str] | None = None, today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    terms = search_terms(conn, extra_terms or [])
    if not terms:
        log.warning("No products or extra terms to search Kroger for; import receipts first")
    counts: dict[str, int] = {}
    for store, location_id in locations.items():
        deals: dict[str, DealIn] = {}
        for term in terms:
            try:
                products = client.search_products(term, location_id)
            except httpx.HTTPError as e:
                log.warning("Kroger search failed for %r at %s: %s", term, store, e)
                continue
            for p in products:
                deal, current, regular = product_to_deal(p, store)
                upc = (p.get("upc") or "").lstrip("0") or None
                if current:
                    record_price(conn, store, item_key(upc, p.get("productId"), p.get("description") or ""),
                                 today, current, regular, "kroger_api")
                if deal:
                    deals[deal.raw_text] = deal
        counts[store] = save_deals(conn, list(deals.values()), week, today)
        log.info("%s: %d promo items", store, counts[store])
    conn.commit()
    return counts
