"""Product matching, in the plan's order:

1. UPC exact match
2. Saved mappings (alias table; every manual fix is stored and reused)
3. Fuzzy similarity on brand, name and size, with a confidence score
4. Optional LLM tie-break for the leftovers; anything still unsure goes to the
   review queue (`grocery review`)
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher

from .normalize import alias_key, norm_brand, normalize_text, parse_size, size_from_parts, tokens
from .repo import PurchaseLine, item_key

log = logging.getLogger(__name__)

AUTO_ACCEPT = 0.85   # at or above: matched automatically
REVIEW_MIN = 0.60    # between REVIEW_MIN and AUTO_ACCEPT: LLM tie-break or review queue
PURCHASE_AUTO = 0.90  # receipts create a new product unless this confident


@dataclass
class Candidate:
    brand: str | None
    name: str
    size: tuple[float, float, str] | None
    raw: str = ""


def _brand_score(a: Candidate, b: Candidate) -> tuple[float, bool]:
    """Returns (score, conflict)."""
    ba, bb = norm_brand(a.brand), norm_brand(b.brand)
    if ba and bb:
        if ba == bb or ba in bb or bb in ba:
            return 1.0, False
        return 0.0, True
    # One side has a brand: look for it in the other side's text.
    if ba and ba in normalize_text(f"{b.raw} {b.name}").replace(" ", ""):
        return 1.0, False
    if bb and bb in normalize_text(f"{a.raw} {a.name}").replace(" ", ""):
        return 1.0, False
    if not ba and not bb:
        return 0.7, False
    return 0.4, False


def _size_score(a, b) -> float:
    if not a or not b:
        return 0.6
    if a[2] != b[2]:
        return 0.5
    # the two size ranges overlap, with 15% tolerance
    if a[0] <= b[1] * 1.15 and b[0] <= a[1] * 1.15:
        return 1.0
    return 0.2


def similarity(a: Candidate, b: Candidate) -> float:
    """Score 0..1 that two descriptions are the same product (a = what you buy)."""
    brand_tokens = tokens(a.brand) | tokens(b.brand)
    ta = tokens(a.name) - brand_tokens
    tb = (tokens(b.name) | (tokens(b.raw) if not b.name else set())) - brand_tokens
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    if not shared:
        return 0.0
    overlap_min = len(shared) / min(len(ta), len(tb))
    coverage = len(shared) / len(ta)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    name = 0.4 * overlap_min + 0.4 * coverage + 0.2 * seq
    brand, conflict = _brand_score(a, b)
    score = 0.55 * name + 0.30 * brand + 0.15 * _size_score(a.size, b.size)
    if conflict:
        score = min(score, 0.5)
    return round(score, 3)


def product_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(row["brand"], row["name"], size_from_parts(row["size_qty"], row["size_unit"]))


def deal_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(row["brand"], row["name"] or row["raw_text"],
                     parse_size(row["size_text"]) or parse_size(row["raw_text"]), row["raw_text"])


def _alias_lookup(conn, *texts: str | None) -> int | None:
    for t in texts:
        if not t:
            continue
        row = conn.execute("SELECT product_id FROM alias WHERE alias_text=?", (alias_key(t),)).fetchone()
        if row:
            return row["product_id"]
    return None


def save_alias(conn, text: str, product_id: int, source: str) -> None:
    key = alias_key(text)
    if key:
        conn.execute(
            "INSERT INTO alias (alias_text, product_id, source) VALUES (?,?,?) "
            "ON CONFLICT (alias_text) DO UPDATE SET product_id=excluded.product_id, source=excluded.source",
            (key, product_id, source),
        )


def _best_product(conn, cand: Candidate, exclude: set[int] = frozenset()) -> tuple[int | None, float]:
    best_id, best = None, 0.0
    for row in conn.execute("SELECT * FROM product"):
        if row["product_id"] in exclude:
            continue
        s = similarity(product_candidate(row), cand)
        if s > best:
            best_id, best = row["product_id"], s
    return best_id, best


def match_purchase_line(conn, line: PurchaseLine) -> tuple[int | None, str | None, float | None]:
    if line.upc:
        row = conn.execute("SELECT product_id FROM product WHERE upc=?", (line.upc.lstrip("0"),)).fetchone()
        if row:
            return row["product_id"], "upc", 1.0
    pid = _alias_lookup(conn, line.raw_name)
    if pid:
        return pid, "alias", 1.0
    cand = Candidate(line.brand, line.name or line.raw_name, size_from_parts(line.size_qty, line.size_unit),
                     line.raw_name)
    pid, score = _best_product(conn, cand)
    if pid and score >= PURCHASE_AUTO:
        return pid, "fuzzy", score
    return None, None, None


def _set_deal_product(conn, deal: sqlite3.Row, product_id: int, method: str, conf: float) -> None:
    conn.execute("UPDATE deal SET product_id=?, match_method=?, match_confidence=? WHERE deal_id=?",
                 (product_id, method, conf, deal["deal_id"]))
    conn.execute("UPDATE price_obs SET product_id=? WHERE store=? AND item_key=? AND product_id IS NULL",
                 (product_id, deal["store"], item_key(deal["upc"], deal["sku"], deal["raw_text"])))


def match_deals(conn: sqlite3.Connection, week: str | None = None, use_llm: bool = False,
                model: str = "claude-opus-5-5") -> dict[str, int]:
    """Match unmatched deals (optionally only one ad week) to canonical products."""
    stats = {"upc": 0, "alias": 0, "fuzzy": 0, "llm": 0, "review": 0, "unmatched": 0}
    sql = """SELECT * FROM deal WHERE product_id IS NULL
             AND deal_id NOT IN (SELECT deal_id FROM match_review)"""
    args: tuple = ()
    if week:
        sql += " AND week_start=?"
        args = (week,)
    rejected: dict[str, set[int]] = {}
    for r in conn.execute("SELECT alias_text, product_id FROM match_reject"):
        rejected.setdefault(r["alias_text"], set()).add(r["product_id"])

    for deal in conn.execute(sql, args).fetchall():
        if deal["upc"]:
            row = conn.execute(
                """SELECT product_id FROM product WHERE upc=:u
                   UNION SELECT product_id FROM store_item WHERE upc=:u AND product_id IS NOT NULL
                   LIMIT 1""", {"u": deal["upc"].lstrip("0")}).fetchone()
            if row:
                _set_deal_product(conn, deal, row["product_id"], "upc", 1.0)
                stats["upc"] += 1
                continue
        pid = _alias_lookup(conn, deal["raw_text"], f"{deal['brand'] or ''} {deal['name'] or ''}".strip())
        if pid:
            _set_deal_product(conn, deal, pid, "alias", 1.0)
            stats["alias"] += 1
            continue

        cand = deal_candidate(deal)
        pid, score = _best_product(conn, cand, rejected.get(alias_key(deal["raw_text"]), set()))
        if pid and score >= AUTO_ACCEPT:
            _set_deal_product(conn, deal, pid, "fuzzy", score)
            stats["fuzzy"] += 1
        elif pid and score >= REVIEW_MIN:
            if use_llm and _llm_confirms(conn, deal, pid, model):
                _set_deal_product(conn, deal, pid, "llm", score)
                stats["llm"] += 1
            else:
                conn.execute("INSERT OR IGNORE INTO match_review (deal_id, product_id, score) VALUES (?,?,?)",
                             (deal["deal_id"], pid, score))
                stats["review"] += 1
        else:
            stats["unmatched"] += 1
    conn.commit()
    return stats


def describe_product(row: sqlite3.Row) -> str:
    size = f" {row['size_qty']:g} {row['size_unit']}" if row["size_qty"] and row["size_unit"] else ""
    brand = f"{row['brand']} " if row["brand"] else ""
    return f"{brand}{row['name']}{size}"


def _llm_confirms(conn, deal, product_id: int, model: str) -> bool:
    from .llm import LLMError, judge_match

    prod = conn.execute("SELECT * FROM product WHERE product_id=?", (product_id,)).fetchone()
    try:
        verdict = judge_match(deal["raw_text"], describe_product(prod), model)
    except LLMError as e:
        log.warning("LLM tie-break failed for deal %s: %s", deal["deal_id"], e)
        return False
    except Exception as e:  # network/auth errors: fall back to the review queue
        log.warning("LLM tie-break unavailable: %s", e)
        return False
    return verdict.same_product


def pending_reviews(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.review_id, r.score, d.deal_id, d.store, d.raw_text, d.sale_price, p.product_id,
                  p.name, p.brand, p.size_qty, p.size_unit
           FROM match_review r JOIN deal d USING (deal_id) JOIN product p ON p.product_id = r.product_id
           WHERE r.status='pending' ORDER BY r.score DESC""").fetchall()


def resolve_review(conn, review_id: int, accept: bool) -> None:
    r = conn.execute(
        """SELECT r.product_id, d.deal_id, d.store, d.upc, d.sku, d.raw_text
           FROM match_review r JOIN deal d USING (deal_id) WHERE r.review_id=?""",
        (review_id,)).fetchone()
    if r is None:
        raise KeyError(review_id)
    if accept:
        _set_deal_product(conn, r, r["product_id"], "manual", 1.0)
        save_alias(conn, r["raw_text"], r["product_id"], "manual")
    else:
        conn.execute("INSERT OR IGNORE INTO match_reject (alias_text, product_id) VALUES (?,?)",
                     (alias_key(r["raw_text"]), r["product_id"]))
    conn.execute("UPDATE match_review SET status=? WHERE review_id=?",
                 ("accepted" if accept else "rejected", review_id))
    conn.commit()


def manual_match(conn, deal_id: int, product_id: int) -> None:
    deal = conn.execute("SELECT * FROM deal WHERE deal_id=?", (deal_id,)).fetchone()
    if deal is None:
        raise KeyError(deal_id)
    _set_deal_product(conn, deal, product_id, "manual", 1.0)
    save_alias(conn, deal["raw_text"], product_id, "manual")
    conn.execute("UPDATE match_review SET status='accepted' WHERE deal_id=? AND status='pending'", (deal_id,))
    conn.commit()
