"""Weekly ad extraction for stores without an API (Safeway, Metropolitan Market).

Point it at the ad as a URL or local file (PDF, image, HTML or text) and the
LLM turns it into structured deal lines. Kept deliberately small so it is easy
to swap for a better source (e.g. a flyer aggregator's JSON) later.
"""

from __future__ import annotations

import logging
from datetime import date

from ..llm import extract_ad, html_to_text, read_source
from ..repo import DealIn, save_deals

log = logging.getLogger(__name__)

# Long HTML pages are split so each request stays well within output limits.
CHUNK_CHARS = 40_000


def _chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    lines, out, cur, n = text.splitlines(), [], [], 0
    for ln in lines:
        if n + len(ln) > size and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def ingest(conn, store: str, source: str, week: date, model: str) -> int:
    data, name = read_source(source)
    is_html = name.lower().endswith((".html", ".htm")) or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))
    if is_html:
        text = html_to_text(data.decode("utf-8", errors="replace"))
        ad_deals = []
        for chunk in _chunks(text):
            ad_deals += extract_ad(chunk.encode(), "ad.txt", store, model, text_hint=chunk)
    else:
        ad_deals = extract_ad(data, name, store, model)
    deals = [
        DealIn(store=store, raw_text=d.raw_text, sale_price=d.sale_price, brand=d.brand, name=d.name,
               size_text=d.size_text, category=d.category, regular_price=d.regular_price,
               price_unit=d.price_unit, deal_limit=d.deal_limit, coupon_required=d.coupon_required,
               source="ad_llm")
        for d in ad_deals
    ]
    n = save_deals(conn, deals, week)
    log.info("%s: %d deals extracted from %s", store, n, name)
    return n
