"""Purchase history import: order-history CSVs, receipt files and Gmail receipts."""

from __future__ import annotations

import csv
import email
import email.policy
import email.utils
import hashlib
import imaplib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .llm import Receipt, extract_receipt, html_to_text
from .repo import PurchaseLine, ReceiptIn, save_receipt

log = logging.getLogger(__name__)

# Sender domains for digital receipts. Kroger banners send from their own domains.
SENDER_STORES = {
    "safeway.com": "safeway",
    "albertsons.com": "safeway",
    "fredmeyer.com": "fred_meyer",
    "qfc.com": "qfc",
    "kroger.com": None,  # shared; store guessed from the receipt body
    "metropolitan-market.com": "metmarket",
    "metropolitanmarket.com": "metmarket",
    "groceryoutlet.com": "grocery_outlet",
}

STORE_WORDS = {
    "safeway": "safeway", "fred meyer": "fred_meyer", "fredmeyer": "fred_meyer",
    "qfc": "qfc", "quality food": "qfc", "metropolitan market": "metmarket",
    "grocery outlet": "grocery_outlet",
}


def guess_store(*texts: str | None) -> str | None:
    for t in texts:
        low = (t or "").lower()
        for word, store in STORE_WORDS.items():
            if word in low:
                return store
    return None


# ---- CSV order history ------------------------------------------------------

_COLS = {
    "date": ("date", "order date", "purchase date", "transaction date"),
    "store": ("store", "banner", "retailer"),
    "item": ("item", "description", "product", "product name", "item name", "name"),
    "qty": ("qty", "quantity", "units"),
    "paid": ("paid", "paid price", "total", "line total", "amount", "price paid", "price"),
    "unit_price": ("unit price", "unit_price", "price each"),
    "regular": ("regular price", "regular_price", "original price", "list price"),
    "upc": ("upc", "gtin"),
    "brand": ("brand",),
    "size": ("size",),
    "category": ("category", "department"),
}


def _col(row: dict, key: str):
    for name in _COLS[key]:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _money(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _date(v: str) -> str | None:
    v = (v or "").strip()
    candidates = [v, v.split(" ")[0], v.split("T")[0]]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"):
        for c in candidates:
            try:
                return datetime.strptime(c, fmt).date().isoformat()
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        return None


def import_csv(conn, path: str | Path, default_store: str | None = None) -> int:
    """Import an order-history export. Rows are grouped into receipts by (store, date)."""
    from .normalize import parse_size

    path = Path(path)
    receipts: dict[tuple[str, str], ReceiptIn] = {}
    file_id = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            d = _date(_col(r, "date") or "")
            store = (_col(r, "store") and guess_store(_col(r, "store"))) or _col(r, "store") or default_store
            item = _col(r, "item")
            qty = _money(_col(r, "qty")) or 1.0
            unit = _money(_col(r, "unit_price"))
            paid = _money(_col(r, "paid"))
            if paid is None and unit is not None:
                paid = unit * qty
            if not (d and store and item and paid is not None):
                continue
            size = parse_size(_col(r, "size") or item)
            key = (store, d)
            rec = receipts.setdefault(key, ReceiptIn(store=store, date=d, source="csv",
                                                     source_ref=f"csv:{file_id}:{store}:{d}"))
            rec.lines.append(PurchaseLine(
                raw_name=item, paid_price=paid, qty=qty, unit_price=unit,
                regular_unit_price=_money(_col(r, "regular")), brand=_col(r, "brand"),
                size_qty=None, size_unit=None, category=_col(r, "category"), upc=_col(r, "upc"),
                name=None,
            ))
            if size:
                rec.lines[-1].size_qty, rec.lines[-1].size_unit = size[0], size[2]
    return sum(save_receipt(conn, rec) for rec in receipts.values())


# ---- LLM-parsed receipts (photo, PDF, text, .eml) ---------------------------

def receipt_to_in(parsed: Receipt, store: str | None, fallback_date: str | None, source: str,
                  source_ref: str) -> ReceiptIn | None:
    store = store or parsed.store
    d = parsed.date or fallback_date
    if not store or not d:
        log.warning("Skipping receipt %s: store=%s date=%s", source_ref, store, d)
        return None
    return ReceiptIn(store=store, date=d, source=source, source_ref=source_ref, lines=[
        PurchaseLine(raw_name=ln.raw_name, paid_price=ln.paid_price, qty=ln.qty or 1.0,
                     unit_price=ln.unit_price, regular_unit_price=ln.regular_unit_price, brand=ln.brand,
                     name=ln.name, size_qty=ln.size_qty, size_unit=ln.size_unit, category=ln.category,
                     upc=ln.upc, shelf_stable=ln.shelf_stable)
        for ln in parsed.lines if ln.paid_price is not None
    ])


def _already_imported(conn, source_ref: str) -> bool:
    return conn.execute("SELECT 1 FROM purchase WHERE source_ref=? LIMIT 1", (source_ref,)).fetchone() is not None


def import_file(conn, path: str | Path, model: str, store: str | None = None) -> int:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return import_csv(conn, path, store)
    data = path.read_bytes()
    if path.suffix.lower() == ".eml":
        return import_email_bytes(conn, data, model, store)
    ref = f"file:{hashlib.sha1(data).hexdigest()[:16]}"
    if _already_imported(conn, ref):
        return 0
    text_hint = html_to_text(data.decode("utf-8", "replace")) if path.suffix.lower() in (".html", ".htm") else None
    parsed = extract_receipt(data, path.name, model, store, text_hint=text_hint)
    rec = receipt_to_in(parsed, store, None, "file", ref)
    return save_receipt(conn, rec) if rec else 0


def _email_body(msg) -> str:
    part = msg.get_body(preferencelist=("html", "plain"))
    if part is None:
        return ""
    content = part.get_content()
    return html_to_text(content) if part.get_content_type() == "text/html" else content


def import_email_bytes(conn, raw: bytes, model: str, store: str | None = None) -> int:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    ref = f"email:{msg.get('Message-ID') or hashlib.sha1(raw).hexdigest()[:16]}"
    if _already_imported(conn, ref):
        return 0
    sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    domain_store = next((s for dom, s in SENDER_STORES.items() if sender.endswith(dom)), None)
    body = _email_body(msg)
    if not body.strip():
        return 0
    store = store or domain_store or guess_store(msg.get("Subject"), body[:2000])
    sent = None
    if msg.get("Date"):
        try:
            sent = email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
        except (TypeError, ValueError):
            pass
    parsed = extract_receipt(body.encode(), "receipt.txt", model, store, text_hint=body)
    rec = receipt_to_in(parsed, store, sent, "email", ref)
    return save_receipt(conn, rec) if rec else 0


def fetch_gmail(conn, user: str, app_password: str, model: str, since_days: int = 30,
                senders: list[str] | None = None) -> dict[str, int]:
    """Pull receipt emails over IMAP (Gmail app password) and import them."""
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    stats = {"emails": 0, "lines": 0, "errors": 0}
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(user, app_password)
        imap.select('"[Gmail]/All Mail"', readonly=True)
        for dom in senders or list(SENDER_STORES):
            typ, data = imap.search(None, "FROM", f'"{dom}"', "SINCE", since)
            if typ != "OK":
                continue
            for num in data[0].split():
                typ, msg_data = imap.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                try:
                    n = import_email_bytes(conn, msg_data[0][1], model)
                except Exception as e:  # keep going; one bad email shouldn't stop the run
                    log.warning("Failed to import email %s from %s: %s", num, dom, e)
                    stats["errors"] += 1
                    continue
                stats["emails"] += 1 if n else 0
                stats["lines"] += n
    return stats
