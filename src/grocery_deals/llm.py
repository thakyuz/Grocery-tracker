"""Claude-based extraction for weekly ads and receipts, plus match tie-breaks.

Uses structured outputs (`messages.parse` with a Pydantic model) so every
response is validated before it touches the database.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class LLMError(RuntimeError):
    pass


# ---- schemas ---------------------------------------------------------------

class AdDeal(BaseModel):
    raw_text: str = Field(description="The deal as printed in the ad, one line")
    brand: Optional[str] = Field(None, description="Brand name, or null for store/produce items")
    name: str = Field(description="Product name without brand or size, e.g. 'sharp cheddar cheese'")
    size_text: Optional[str] = Field(None, description="Size as printed, e.g. '7-8 oz', '1 lb', '12 ct'")
    category: Optional[str] = Field(None, description="Short grocery category, e.g. dairy, produce, meat, coffee")
    sale_price: float = Field(description="Effective price for ONE unit. '2 for $5' -> 2.50; '$3.99/lb' -> 3.99")
    price_unit: Optional[str] = Field(None, description="'each' or 'lb' (or another unit if priced by weight/volume)")
    regular_price: Optional[float] = Field(
        None, description="Regular price for one unit if shown (including 'compare at' / 'elsewhere' prices), else null")
    deal_limit: Optional[int] = Field(None, description="Purchase limit if stated")
    coupon_required: bool = Field(False, description="True if a digital coupon, loyalty card clip or 'with coupon' is required")


class AdExtraction(BaseModel):
    deals: list[AdDeal]


class ReceiptLine(BaseModel):
    raw_name: str = Field(description="Item text exactly as printed on the receipt")
    brand: Optional[str] = Field(None, description="Brand, expanded from abbreviations when clear (TLMK -> Tillamook)")
    name: str = Field(description="Readable product name without brand or size, abbreviations expanded")
    size_qty: Optional[float] = Field(None, description="Package size number, e.g. 8 for '8OZ'")
    size_unit: Optional[str] = Field(None, description="Package size unit: oz, fl oz, lb, gal, ct, ...")
    category: Optional[str] = Field(None, description="Short grocery category")
    upc: Optional[str] = Field(None, description="UPC if printed")
    qty: float = Field(1, description="Quantity bought (units, or pounds for weighed items)")
    unit_price: Optional[float] = Field(None, description="Price paid per unit after discounts")
    paid_price: float = Field(description="Line total actually paid after discounts")
    regular_unit_price: Optional[float] = Field(None, description="Regular price per unit before card savings, if shown")
    shelf_stable: bool = Field(False, description="True for pantry/frozen items that keep for months")


class Receipt(BaseModel):
    store: Optional[str] = Field(None, description="One of: safeway, fred_meyer, qfc, metmarket, grocery_outlet, or null")
    date: Optional[str] = Field(None, description="Purchase date YYYY-MM-DD")
    lines: list[ReceiptLine] = Field(description="Only purchased items: skip totals, tax, payment, coupons-only lines")


class MatchVerdict(BaseModel):
    same_product: bool = Field(description="True if the ad deal covers the product the household buys")
    reason: str


# ---- client ----------------------------------------------------------------

def _client():
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMError("Install the 'anthropic' package to use extraction") from e
    return anthropic.Anthropic()


def _content_from_source(data: bytes, filename: str, text_hint: str | None = None) -> list[dict]:
    """Build a message content block for a PDF, image or text/HTML file."""
    mime = mimetypes.guess_type(filename)[0] or ""
    if mime == "application/pdf" or data[:4] == b"%PDF":
        return [{"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": base64.b64encode(data).decode()}}]
    if mime.startswith("image/"):
        return [{"type": "image", "source": {
            "type": "base64", "media_type": mime,
            "data": base64.b64encode(data).decode()}}]
    text = text_hint if text_hint is not None else data.decode("utf-8", errors="replace")
    return [{"type": "text", "text": f"<document>\n{text}\n</document>"}]


def _parse(model: str, content: list[dict], schema: type[BaseModel], instructions: str, max_tokens: int = 16000):
    client = _client()
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content + [{"type": "text", "text": instructions}]}],
        output_format=schema,
    )
    if response.stop_reason == "refusal":
        raise LLMError("The model declined this request")
    if response.stop_reason == "max_tokens":
        raise LLMError("Output was cut off (max_tokens); split the input into smaller pieces")
    if response.parsed_output is None:
        raise LLMError("No structured output returned")
    return response.parsed_output


AD_INSTRUCTIONS = """This is a grocery store's weekly ad for {store}.
List every individual deal. Rules:
- One entry per product line; split combined offers ("Tillamook Cheese or Butter") only if prices differ.
- sale_price is the effective price for ONE unit (multi-buy "2/$5" -> 2.50; "Buy 1 Get 1" -> half the regular price if shown).
- If the ad shows a "compare at", "elsewhere" or "retail" price instead of a regular price, use it as regular_price.
- Mark coupon_required when the price needs a digital coupon, loyalty clip or "with coupon".
- Skip non-grocery items, store services and generic banners."""


def extract_ad(data: bytes, filename: str, store: str, model: str, text_hint: str | None = None) -> list[AdDeal]:
    content = _content_from_source(data, filename, text_hint)
    result = _parse(model, content, AdExtraction, AD_INSTRUCTIONS.format(store=store))
    return result.deals


RECEIPT_INSTRUCTIONS = """This is a grocery receipt or order confirmation{store_hint}.
Extract each purchased item line. Expand store abbreviations into a readable
brand and name ("TLMK CHED SHRP 8OZ" -> brand Tillamook, name sharp cheddar
cheese, size 8 oz). Put card/member savings into regular_unit_price when the
receipt shows the pre-discount price. Skip subtotal, tax, bag fee, payment and
standalone coupon lines."""


def extract_receipt(data: bytes, filename: str, model: str, store: str | None = None,
                    text_hint: str | None = None) -> Receipt:
    content = _content_from_source(data, filename, text_hint)
    hint = f" from {store}" if store else ""
    return _parse(model, content, Receipt, RECEIPT_INSTRUCTIONS.format(store_hint=hint))


def judge_match(deal_text: str, product_desc: str, model: str) -> MatchVerdict:
    content = [{"type": "text", "text": (
        f"Ad deal: {deal_text}\nProduct the household buys: {product_desc}\n"
        "Would buying from this deal give them that product (same brand when the "
        "product has one, compatible size/variety)?")}]
    return _parse(model, content, MatchVerdict, "Answer with the schema.", max_tokens=2000)


def read_source(path_or_url: str) -> tuple[bytes, str]:
    """Load an ad/receipt from a local path or http(s) URL."""
    if path_or_url.startswith(("http://", "https://")):
        import httpx

        r = httpx.get(path_or_url, follow_redirects=True, timeout=60,
                      headers={"User-Agent": "grocery-deals/0.1 (personal use)"})
        r.raise_for_status()
        name = path_or_url.split("?")[0].rstrip("/").split("/")[-1] or "ad"
        ctype = r.headers.get("content-type", "")
        if "pdf" in ctype and not name.endswith(".pdf"):
            name += ".pdf"
        elif "html" in ctype and not name.endswith((".html", ".htm")):
            name += ".html"
        return r.content, name
    p = Path(path_or_url)
    return p.read_bytes(), p.name


def html_to_text(html: str) -> str:
    import html as html_mod
    import re

    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html)
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", html))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)
