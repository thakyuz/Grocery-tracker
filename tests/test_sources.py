from datetime import date

import httpx

from grocery_deals import llm, receipts
from grocery_deals.sources import csv_deals, flyer, kroger

WEEK = date(2026, 11, 4)


def test_kroger_product_to_deal():
    p = {"productId": "0001", "upc": "0001111041700", "brand": "Kroger", "description": "Kroger 2% Milk",
         "categories": ["Dairy"], "items": [{"size": "1 gal", "price": {"regular": 3.99, "promo": 2.99}}]}
    deal, current, regular = kroger.product_to_deal(p, "qfc")
    assert deal.sale_price == 2.99 and deal.regular_price == 3.99 and deal.upc == "1111041700"
    assert current == 2.99 and deal.category == "dairy"
    p["items"][0]["price"]["promo"] = 0
    deal, current, _ = kroger.product_to_deal(p, "qfc")
    assert deal is None and current == 3.99


def test_kroger_ingest_with_mock_transport(conn):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 1800})
        loc = request.url.params["filter.locationId"]
        promo = 4.49 if loc == "701" else 0
        return httpx.Response(200, json={"data": [
            {"productId": "p1", "upc": "0007", "brand": "Tillamook", "description": "Tillamook Sharp Cheddar",
             "items": [{"size": "8 oz", "price": {"regular": 5.49, "promo": promo}}]}]})

    client = kroger.KrogerClient("id", "secret", httpx.Client(transport=httpx.MockTransport(handler)))
    counts = kroger.ingest(conn, client, {"fred_meyer": "701", "qfc": "705"}, WEEK, ["cheddar"], WEEK)
    assert counts == {"fred_meyer": 1, "qfc": 0}
    assert conn.execute("SELECT COUNT(*) FROM price_obs WHERE source='kroger_api'").fetchone()[0] == 2
    assert calls.count("/v1/connect/oauth2/token") == 1


def test_flyer_ingest_uses_llm_output(conn, tmp_path, monkeypatch):
    ad = tmp_path / "ad.html"
    ad.write_text("<html><body><p>Rao's Sauce 24 oz $6.99</p><script>x()</script></body></html>")
    seen = {}

    def fake_extract(data, filename, store, model, text_hint=None):
        seen["text"] = text_hint
        return [llm.AdDeal(raw_text="Rao's Sauce 24 oz", brand="Rao's", name="sauce", size_text="24 oz",
                           sale_price=6.99, regular_price=8.49)]

    monkeypatch.setattr(flyer, "extract_ad", fake_extract)
    assert flyer.ingest(conn, "metmarket", str(ad), WEEK, "m") == 1
    assert "x()" not in seen["text"] and "Rao's Sauce" in seen["text"]


def test_csv_deals(conn, tmp_path):
    f = tmp_path / "deals.csv"
    f.write_text("Store,Item,Sale_Price,Regular_Price,Coupon\nsafeway,Lucerne Eggs 12 ct,$2.99,4.49,yes\n"
                 "safeway,,1.00,,\n")
    assert csv_deals.ingest(conn, f, WEEK) == 1
    row = conn.execute("SELECT * FROM deal").fetchone()
    assert row["coupon_required"] == 1 and row["regular_price"] == 4.49


def test_order_history_csv(conn, tmp_path):
    f = tmp_path / "orders.csv"
    f.write_text("Order Date,Store,Description,Quantity,Price,Regular Price\n"
                 "10/01/2026,Fred Meyer,Tillamook Sharp Cheddar 8 oz,2,7.98,4.99\n"
                 "10/01/2026,Fred Meyer,Bananas,1,0.79,\n"
                 "2026-10-08,QFC,Tillamook Sharp Cheddar 8 oz,1,4.99,\n")
    assert receipts.import_csv(conn, f) == 3
    assert receipts.import_csv(conn, f) == 0  # idempotent
    stores = {r[0] for r in conn.execute("SELECT DISTINCT store FROM purchase")}
    assert stores == {"fred_meyer", "qfc"}
    unit = conn.execute("SELECT unit_price FROM purchase WHERE qty=2").fetchone()[0]
    assert unit == 3.99


def test_email_receipt(conn, monkeypatch):
    raw = (b"From: Safeway <receipts@email.safeway.com>\r\nSubject: Your receipt\r\n"
           b"Date: Mon, 02 Nov 2026 10:00:00 -0800\r\nMessage-ID: <abc@x>\r\n"
           b"Content-Type: text/html\r\n\r\n<p>TLMK CHED SHRP 8OZ 3.99</p>")

    def fake(data, filename, model, store=None, text_hint=None):
        assert store == "safeway" and "TLMK" in text_hint
        return llm.Receipt(store=None, date=None, lines=[llm.ReceiptLine(
            raw_name="TLMK CHED SHRP 8OZ", brand="Tillamook", name="sharp cheddar cheese", size_qty=8,
            size_unit="oz", paid_price=3.99)])

    monkeypatch.setattr(receipts, "extract_receipt", fake)
    assert receipts.import_email_bytes(conn, raw, "m") == 1
    assert receipts.import_email_bytes(conn, raw, "m") == 0
    row = conn.execute("SELECT date, store FROM purchase").fetchone()
    assert tuple(row) == ("2026-11-02", "safeway")


def test_grocery_outlet_configured_and_receipts_recognized(monkeypatch):
    from grocery_deals.config import Settings

    monkeypatch.setenv("GROCERY_OUTLET_AD_SOURCE", "https://example.com/go-ad.pdf")
    s = Settings.from_env()
    assert s.ad_sources["grocery_outlet"] == "https://example.com/go-ad.pdf"
    assert "grocery_outlet" in s.configured_stores()
    assert receipts.guess_store("Thanks for shopping at Grocery Outlet Bargain Market") == "grocery_outlet"
    assert receipts.SENDER_STORES["groceryoutlet.com"] == "grocery_outlet"


def test_flyer_ingest_grocery_outlet_compare_at(conn, monkeypatch, tmp_path):
    ad = tmp_path / "go.pdf"
    ad.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(flyer, "extract_ad", lambda *a, **k: [llm.AdDeal(
        raw_text="Tillamook Ice Cream 48 oz, compare at $7.49", brand="Tillamook", name="ice cream",
        size_text="48 oz", sale_price=3.99, regular_price=7.49)])
    assert flyer.ingest(conn, "grocery_outlet", str(ad), WEEK, "m") == 1
    row = conn.execute("SELECT store, regular_price FROM deal").fetchone()
    assert tuple(row) == ("grocery_outlet", 7.49)
