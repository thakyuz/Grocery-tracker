from datetime import date

from grocery_deals.normalize import normalize_text, parse_size, tokens, week_start


def test_abbreviations_expand():
    assert normalize_text("TLMK CHED SHRP 8OZ") == "tillamook cheddar sharp"


def test_tokens_drop_stopwords_and_plurals():
    assert tokens("Organic Bananas, select varieties") == {"organic", "banana"}


def test_parse_size_ranges_and_units():
    assert parse_size("Tillamook Cheese 7-8 oz") == (7.0, 8.0, "oz")
    lo, hi, unit = parse_size("1 lb")
    assert (lo, hi, unit) == (16.0, 16.0, "oz")
    assert parse_size("12 fl oz")[2] == "floz"
    assert parse_size("1 gal")[0] == 128.0
    assert parse_size("no size here") is None


def test_week_start_is_wednesday():
    assert week_start(date(2026, 11, 4)) == date(2026, 11, 4)   # Wednesday
    assert week_start(date(2026, 11, 10)) == date(2026, 11, 4)  # Tuesday
    assert week_start(date(2026, 11, 11)) == date(2026, 11, 11)
