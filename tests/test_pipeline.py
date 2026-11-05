from datetime import date

from grocery_deals import cadence, matching
from grocery_deals.demo import seed
from grocery_deals.digest import build_digest, render_html, render_text
from grocery_deals.repo import PurchaseLine, ReceiptIn, save_receipt
from grocery_deals.savings import monthly_savings
from grocery_deals.scoring import due_factor, frequency_factor

TODAY = date(2026, 11, 5)
WEEK = "2026-11-04"


def test_cadence(conn):
    for i, d in enumerate(["2026-09-01", "2026-09-22", "2026-10-13", "2026-11-03"]):
        save_receipt(conn, ReceiptIn("safeway", d, "t", f"r{i}", [
            PurchaseLine("COFFEE", 10.0, qty=2 if i == 3 else 1, unit_price=10.0 if i else 9.0,
                         name="coffee")]))
    cadence.rebuild_cadence(conn, TODAY)
    c = conn.execute("SELECT * FROM product_cadence").fetchone()
    assert c["avg_days_between"] == 21.0
    assert c["last_bought"] == "2026-11-03"
    assert c["purchases_6mo"] == 4
    assert c["typical_price"] == 10.0
    assert c["typical_qty"] == 1


def test_scoring_factors():
    assert frequency_factor(0) == 0
    assert frequency_factor(12) == 1.0
    assert frequency_factor(30) == 1.0
    due, advice = due_factor(20, 21, False)
    assert due > 0.9 and advice.startswith("due now")
    early, advice = due_factor(2, 21, True)
    assert early == 0.8 and "stock up" in advice
    early, advice = due_factor(2, 21, False)
    assert early < 0.5 and advice.startswith("not due")


def test_demo_end_to_end(conn):
    seed(conn, today=TODAY)
    cadence.rebuild_cadence(conn, TODAY)
    stats = matching.match_deals(conn, WEEK)
    assert stats["fuzzy"] >= 4
    d = build_digest(conn, WEEK, ["safeway", "fred_meyer", "qfc", "metmarket"], today=TODAY)
    assert len(d.tier1) >= 4
    assert d.tier1 == sorted(d.tier1, key=lambda i: i.score, reverse=True)
    assert all(i.discount > 0 for i in d.tier1)
    assert not d.alerts
    assert any("confirm with `grocery review`" in i.advice for i in d.tier2)
    text, page = render_text(d), render_html(d)
    assert "Your items on sale" in text and "<table>" in page
    assert conn.execute("SELECT COUNT(*) FROM digest_item WHERE week_start=?", (WEEK,)).fetchone()[0] == len(d.items)


def test_zero_deal_alert(conn):
    seed(conn, today=TODAY)
    d = build_digest(conn, "2026-11-11", ["safeway"], today=TODAY)
    assert d.alerts and "Safeway" in d.alerts[0]


def test_not_a_real_sale_is_skipped(conn):
    from grocery_deals.repo import DealIn, save_deals

    save_receipt(conn, ReceiptIn("qfc", "2026-10-01", "t", "r", [
        PurchaseLine("FAGE", 3.99, unit_price=3.99, brand="Fage", name="total greek yogurt", size_qty=32,
                     size_unit="oz")]))
    save_deals(conn, [DealIn("qfc", "Fage Total Greek Yogurt 32 oz", 4.99, "Fage", "total greek yogurt",
                             "32 oz", regular_price=6.49)], date(2026, 11, 4))
    cadence.rebuild_cadence(conn, TODAY)
    matching.match_deals(conn, WEEK)
    d = build_digest(conn, WEEK, [], today=TODAY)
    assert d.tier1 == []  # $4.99 "sale" is more than the $3.99 you usually pay


def test_savings_counts_digest_picks(conn):
    seed(conn, today=TODAY)
    cadence.rebuild_cadence(conn, TODAY)
    matching.match_deals(conn, WEEK)
    d = build_digest(conn, WEEK, [], today=TODAY)
    pick = d.tier1[0]
    raw = conn.execute(
        """SELECT si.raw_name, si.store FROM store_item si JOIN deal d ON d.product_id = si.product_id
           AND d.store = si.store WHERE d.deal_id=?""", (pick.deal_id,)).fetchone()
    save_receipt(conn, ReceiptIn(raw["store"], "2026-11-06", "t", "after", [
        PurchaseLine(raw["raw_name"], pick.sale_price, unit_price=pick.sale_price,
                     regular_unit_price=pick.reference_price)]))
    nov = monthly_savings(conn)[0]
    assert nov.month == "2026-11" and nov.digest_hits == 1 and nov.digest_savings > 0
