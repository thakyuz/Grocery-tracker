"""Text normalization, size parsing and ad-week helpers."""

from __future__ import annotations

import re
from datetime import date, timedelta

# Common receipt abbreviations. Receipt lines that go through the LLM parser
# come back expanded; this covers CSV imports and the fuzzy matcher.
ABBREVIATIONS = {
    "chs": "cheese", "ched": "cheddar", "shrp": "sharp", "mlk": "milk",
    "org": "organic", "orgnc": "organic", "grk": "greek", "ygrt": "yogurt",
    "yog": "yogurt", "bnls": "boneless", "sknls": "skinless", "chkn": "chicken",
    "brst": "breast", "bf": "beef", "grnd": "ground", "wht": "white",
    "whl": "whole", "brd": "bread", "bnna": "banana", "bnnas": "bananas",
    "cof": "coffee", "cofe": "coffee", "veg": "vegetable", "pnut": "peanut",
    "btr": "butter", "bttr": "butter", "crm": "cream", "frz": "frozen",
    "jce": "juice", "oj": "orange juice", "pb": "peanut butter", "tom": "tomato",
    "tlmk": "tillamook", "dz": "dozen", "lg": "large", "sm": "small",
    "reg": "regular", "unswt": "unsweetened", "swt": "sweet", "van": "vanilla",
    "choc": "chocolate", "strwb": "strawberry", "blueb": "blueberry",
}

STOPWORDS = {
    "the", "and", "or", "of", "with", "a", "an", "for", "select", "varieties",
    "variety", "assorted", "pack", "pk", "ct", "count", "each", "ea", "lb",
    "lbs", "oz", "fl", "gal", "gallon", "qt", "pt", "ml", "l", "g", "kg",
    "sale", "fresh", "new", "only", "per", "or",
}

UNIT_ALIASES = {
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "fl oz": "floz", "floz": "floz", "fl. oz": "floz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "gal": "gal", "gallon": "gal", "qt": "qt", "quart": "qt", "pt": "pt",
    "ml": "ml", "l": "l", "liter": "l", "litre": "l", "g": "g", "gram": "g",
    "kg": "kg", "ct": "ct", "count": "ct", "pk": "ct", "pack": "ct", "dozen": "ct",
}

# Convert to a base unit so 1 lb and 16 oz compare equal.
TO_BASE = {
    "oz": ("oz", 1.0), "lb": ("oz", 16.0), "g": ("oz", 0.035274), "kg": ("oz", 35.274),
    "floz": ("floz", 1.0), "gal": ("floz", 128.0), "qt": ("floz", 32.0),
    "pt": ("floz", 16.0), "ml": ("floz", 0.033814), "l": ("floz", 33.814),
    "ct": ("ct", 1.0),
}

_SIZE_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(?P<hi>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>fl\.?\s?oz|ounces?|oz|lbs?|pounds?|gallons?|gal|quarts?|qt|pt|ml|"
    r"liters?|litres?|l|kg|grams?|g|ct|count|pk|pack|dozen)\b",
    re.IGNORECASE,
)


def normalize_text(text: str | None) -> str:
    """Lowercase, strip punctuation and sizes, expand abbreviations."""
    if not text:
        return ""
    t = text.lower().replace("&", " and ")
    t = _SIZE_RE.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    words = []
    for w in t.split():
        w = ABBREVIATIONS.get(w, w)
        words.extend(w.split())
    return " ".join(words)


def tokens(text: str | None) -> set[str]:
    out = set()
    for w in normalize_text(text).split():
        if w in STOPWORDS or w.isdigit() or len(w) < 2:
            continue
        # crude singularization so "bananas" ~ "banana"
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def parse_size(text: str | None) -> tuple[float, float, str] | None:
    """Return (low, high, base_unit) for '8 oz', '7-8 oz', '1 lb', '12 ct'."""
    if not text:
        return None
    m = _SIZE_RE.search(text)
    if not m:
        return None
    unit_raw = re.sub(r"[\s.]", "", m.group("unit").lower())
    unit = UNIT_ALIASES.get(unit_raw) or UNIT_ALIASES.get(unit_raw.rstrip("s"))
    if unit_raw.startswith("floz"):
        unit = "floz"
    if not unit:
        return None
    base, factor = TO_BASE[unit]
    lo = float(m.group("lo"))
    hi = float(m.group("hi")) if m.group("hi") else lo
    if m.group("unit").lower() == "dozen":
        lo, hi = lo * 12, hi * 12
    return lo * factor, hi * factor, base


def size_from_parts(qty: float | None, unit: str | None) -> tuple[float, float, str] | None:
    if qty is None or not unit:
        return None
    return parse_size(f"{qty} {unit}")


def norm_brand(brand: str | None) -> str:
    return normalize_text(brand).replace(" ", "")


def week_start(d: date | None = None) -> date:
    """Weekly ads run Wednesday to Tuesday; return the Wednesday starting d's ad week."""
    d = d or date.today()
    return d - timedelta(days=(d.weekday() - 2) % 7)


def alias_key(text: str) -> str:
    """Saved-mapping key. Store-agnostic: the same words mean the same product."""
    return normalize_text(text)
