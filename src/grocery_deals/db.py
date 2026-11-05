"""SQLite storage. The schema mirrors the plan's core tables.

SQLite keeps the MVP dependency-free on the home lab; every query is plain SQL,
so moving to Postgres later is a connection swap plus small dialect fixes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS product (
    product_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    brand        TEXT,
    size_qty     REAL,
    size_unit    TEXT,
    category     TEXT,
    upc          TEXT UNIQUE,
    shelf_stable INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One store's listing of an item (what receipts call it).
CREATE TABLE IF NOT EXISTS store_item (
    store_item_id    INTEGER PRIMARY KEY,
    store            TEXT NOT NULL,
    raw_name         TEXT NOT NULL,
    sku              TEXT,
    upc              TEXT,
    product_id       INTEGER REFERENCES product(product_id),
    match_method     TEXT,
    match_confidence REAL,
    UNIQUE (store, raw_name)
);

-- One receipt / order line.
CREATE TABLE IF NOT EXISTS purchase (
    purchase_id        INTEGER PRIMARY KEY,
    date               TEXT NOT NULL,
    store              TEXT NOT NULL,
    store_item_id      INTEGER NOT NULL REFERENCES store_item(store_item_id),
    qty                REAL NOT NULL DEFAULT 1,
    unit_price         REAL,
    paid_price         REAL,
    regular_unit_price REAL,
    source             TEXT NOT NULL,
    source_ref         TEXT NOT NULL,
    line_no            INTEGER NOT NULL,
    UNIQUE (source_ref, line_no)
);

-- One ad / promo line per store per ad week (week_start = Wednesday).
CREATE TABLE IF NOT EXISTS deal (
    deal_id          INTEGER PRIMARY KEY,
    store            TEXT NOT NULL,
    week_start       TEXT NOT NULL,
    raw_text         TEXT NOT NULL,
    brand            TEXT,
    name             TEXT,
    size_text        TEXT,
    category         TEXT,
    sale_price       REAL NOT NULL,
    regular_price    REAL,
    price_unit       TEXT,
    deal_limit       INTEGER,
    coupon_required  INTEGER NOT NULL DEFAULT 0,
    upc              TEXT,
    sku              TEXT,
    product_id       INTEGER REFERENCES product(product_id),
    match_method     TEXT,
    match_confidence REAL,
    source           TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (store, week_start, raw_text)
);

-- Every price we see, collected from day one (it can't be backfilled).
CREATE TABLE IF NOT EXISTS price_obs (
    price_obs_id  INTEGER PRIMARY KEY,
    store         TEXT NOT NULL,
    item_key      TEXT NOT NULL,
    product_id    INTEGER REFERENCES product(product_id),
    date          TEXT NOT NULL,
    price         REAL NOT NULL,
    regular_price REAL,
    source        TEXT NOT NULL,
    UNIQUE (store, item_key, date, source)
);

-- Saved mappings: every manual fix (and every confident match) is reused.
CREATE TABLE IF NOT EXISTS alias (
    alias_text TEXT PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(product_id),
    source     TEXT NOT NULL
);

-- Matches below the auto-accept threshold wait here for a yes/no.
CREATE TABLE IF NOT EXISTS match_review (
    review_id  INTEGER PRIMARY KEY,
    deal_id    INTEGER NOT NULL UNIQUE REFERENCES deal(deal_id),
    product_id INTEGER NOT NULL REFERENCES product(product_id),
    score      REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Deals rejected for a product, so they aren't proposed again.
CREATE TABLE IF NOT EXISTS match_reject (
    alias_text TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    PRIMARY KEY (alias_text, product_id)
);

-- Derived per product (rebuilt by `grocery cadence`).
CREATE TABLE IF NOT EXISTS product_cadence (
    product_id       INTEGER PRIMARY KEY REFERENCES product(product_id),
    purchase_days    INTEGER NOT NULL,
    purchases_6mo    INTEGER NOT NULL,
    avg_days_between REAL,
    last_bought      TEXT,
    typical_qty      REAL,
    typical_price    REAL
);

-- What each digest recommended, for the savings tracker.
CREATE TABLE IF NOT EXISTS digest_item (
    week_start TEXT NOT NULL,
    deal_id    INTEGER NOT NULL REFERENCES deal(deal_id),
    product_id INTEGER,
    tier       INTEGER NOT NULL,
    score      REAL NOT NULL,
    est_saving REAL,
    PRIMARY KEY (week_start, deal_id)
);

CREATE TABLE IF NOT EXISTS digest_log (
    week_start TEXT PRIMARY KEY,
    sent_at    TEXT,
    n_items    INTEGER,
    alerts     TEXT
);

CREATE INDEX IF NOT EXISTS ix_purchase_item ON purchase(store_item_id);
CREATE INDEX IF NOT EXISTS ix_deal_week ON deal(week_start);
CREATE INDEX IF NOT EXISTS ix_price_obs_product ON price_obs(product_id, date);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
