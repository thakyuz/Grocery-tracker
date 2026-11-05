from datetime import date

from grocery_deals import matching
from grocery_deals.matching import Candidate, similarity
from grocery_deals.normalize import parse_size
from grocery_deals.repo import DealIn, PurchaseLine, ReceiptIn, save_deals, save_receipt

WEEK = date(2026, 11, 4)


def _buy(conn, raw, brand, name, qty=None, unit=None, upc=None, d="2026-10-01", store="safeway", ref="r1", price=4.99):
    save_receipt(conn, ReceiptIn(store, d, "test", ref, [PurchaseLine(
        raw_name=raw, paid_price=price, brand=brand, name=name, size_qty=qty, size_unit=unit, upc=upc)]))


def test_similarity_brand_conflict_is_capped():
    a = Candidate("Tillamook", "sharp cheddar cheese", parse_size("8 oz"))
    b = Candidate("Lucerne", "cheddar cheese", parse_size("8 oz"))
    assert similarity(a, b) <= 0.5


def test_similarity_same_product_scores_high():
    a = Candidate("Fage", "total 0% greek yogurt", parse_size("32 oz"))
    b = Candidate("Fage", "total greek yogurt", parse_size("32 oz"), "Fage Total Greek Yogurt 32 oz")
    assert similarity(a, b) >= matching.AUTO_ACCEPT


def test_similarity_size_mismatch_lowers_score():
    a = Candidate("Fage", "greek yogurt", parse_size("32 oz"))
    same = Candidate("Fage", "greek yogurt", parse_size("32 oz"))
    small = Candidate("Fage", "greek yogurt", parse_size("5.3 oz"))
    assert similarity(a, same) > similarity(a, small)


def test_receipt_lines_reuse_products(conn):
    _buy(conn, "FAGE TOTAL 0% 32OZ", "Fage", "total 0% greek yogurt", 32, "oz", ref="a")
    _buy(conn, "FAGE TOTAL 0% 32OZ", "Fage", "total 0% greek yogurt", 32, "oz", ref="b", d="2026-10-10")
    _buy(conn, "Fage Total 0% Greek Yogurt", "Fage", "total 0% greek yogurt", 32, "oz", ref="c", store="qfc")
    assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM purchase").fetchone()[0] == 3


def test_reimport_is_idempotent(conn):
    _buy(conn, "X", None, "thing", ref="same")
    _buy(conn, "X", None, "thing", ref="same")
    assert conn.execute("SELECT COUNT(*) FROM purchase").fetchone()[0] == 1


def test_match_order_upc_then_fuzzy_then_review(conn):
    _buy(conn, "LACROIX LIME", "LaCroix", "lime sparkling water", 8, "ct", upc="0012993441012", ref="a")
    _buy(conn, "FAGE", "Fage", "total 0% greek yogurt", 32, "oz", ref="b")
    _buy(conn, "BJ HALF BAKED", "Ben & Jerry's", "half baked ice cream", 16, "oz", ref="c")
    save_deals(conn, [
        DealIn("safeway", "La Croix 8pk any flavor", 3.33, upc="12993441012"),
        DealIn("qfc", "Fage Total Greek Yogurt 32 oz", 4.99, "Fage", "total greek yogurt", "32 oz"),
        DealIn("qfc", "Ben & Jerry's Pint", 3.99, "Ben & Jerry's", "ice cream", "16 oz"),
        DealIn("qfc", "Charmin Toilet Paper", 9.99, "Charmin", "toilet paper"),
    ], WEEK)
    stats = matching.match_deals(conn, WEEK.isoformat())
    assert stats["upc"] == 1 and stats["fuzzy"] == 1 and stats["review"] == 1 and stats["unmatched"] == 1


def test_review_accept_saves_alias_for_next_week(conn):
    _buy(conn, "BJ HALF BAKED", "Ben & Jerry's", "half baked ice cream", 16, "oz")
    deal = DealIn("qfc", "Ben & Jerry's Pint", 3.99, "Ben & Jerry's", "ice cream", "16 oz")
    save_deals(conn, [deal], WEEK)
    matching.match_deals(conn, WEEK.isoformat())
    (review,) = matching.pending_reviews(conn)
    matching.resolve_review(conn, review["review_id"], accept=True)
    assert conn.execute("SELECT match_method FROM deal").fetchone()[0] == "manual"

    next_week = date(2026, 11, 11)
    save_deals(conn, [deal], next_week)
    stats = matching.match_deals(conn, next_week.isoformat())
    assert stats["alias"] == 1


def test_review_reject_is_remembered(conn):
    _buy(conn, "BJ HALF BAKED", "Ben & Jerry's", "half baked ice cream", 16, "oz")
    deal = DealIn("qfc", "Ben & Jerry's Pint", 3.99, "Ben & Jerry's", "ice cream", "16 oz")
    save_deals(conn, [deal], WEEK)
    matching.match_deals(conn, WEEK.isoformat())
    (review,) = matching.pending_reviews(conn)
    matching.resolve_review(conn, review["review_id"], accept=False)
    next_week = date(2026, 11, 11)
    save_deals(conn, [deal], next_week)
    stats = matching.match_deals(conn, next_week.isoformat())
    assert stats["review"] == 0 and stats["unmatched"] == 1


def test_llm_tiebreak_used_when_enabled(conn, monkeypatch):
    from grocery_deals import llm

    _buy(conn, "BJ HALF BAKED", "Ben & Jerry's", "half baked ice cream", 16, "oz")
    save_deals(conn, [DealIn("qfc", "Ben & Jerry's Pint", 3.99, "Ben & Jerry's", "ice cream", "16 oz")], WEEK)
    monkeypatch.setattr(llm, "judge_match", lambda *a, **k: llm.MatchVerdict(same_product=True, reason="pint"))
    stats = matching.match_deals(conn, WEEK.isoformat(), use_llm=True)
    assert stats["llm"] == 1


def test_matching_backfills_price_history(conn):
    _buy(conn, "FAGE", "Fage", "total 0% greek yogurt", 32, "oz")
    save_deals(conn, [DealIn("qfc", "Fage Total Greek Yogurt 32 oz", 4.99, "Fage", "total greek yogurt", "32 oz")],
               WEEK, WEEK)
    matching.match_deals(conn, WEEK.isoformat())
    n = conn.execute("SELECT COUNT(*) FROM price_obs WHERE product_id IS NOT NULL AND source='manual'").fetchone()[0]
    assert n == 1
