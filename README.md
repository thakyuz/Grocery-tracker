# Grocery Deals Tracker

A personal app that pulls weekly deals from **Safeway, Fred Meyer, QFC,
Metropolitan Market and Grocery Outlet**, matches them against your purchase history, and sends a
Wednesday-morning digest of *your* items on sale, ranked by savings.

This is the MVP from the "Grocery Deals App — Project Plan": weekly deal
ingestion, purchase-history import, matching, scoring, an email digest, price
history from day one, and a basic list page.

```
receipts (CSV / Gmail / photos)  ─┐
Kroger API (Fred Meyer, QFC)     ─┼─► SQLite ─► match ─► cadence ─► score ─► digest (email + HTML)
Safeway / Met Market /            │      ▲                                    │
  Grocery Outlet ads (LLM)       ─┘      └───── your review answers ◄─────────┘
```

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
grocery --db out/demo.db demo        # sample household + one week of ads, no credentials needed
grocery serve --port 8000            # basic web page (uses $GROCERY_DB)
```

## Setup for real data

1. `cp .env.example .env` and fill in what you use.
2. **Purchase history** (do this first; cadence needs it):
   - Order-history exports: `grocery import-receipts exports/*.csv`
     (columns like *Date, Store, Description, Quantity, Price, Regular Price*; see `examples/order_history.csv`).
   - Receipt photos, PDFs, saved `.eml` files: `grocery import-receipts receipts/` (Claude parses them).
   - Gmail digital receipts over IMAP with an app password: `grocery fetch-gmail --days 365` for the 12-month backfill.
3. **Fred Meyer + QFC**: register an app at developer.kroger.com, set `KROGER_CLIENT_ID/SECRET`,
   find your stores with `grocery kroger-locations --zip 98101`, and set `KROGER_LOCATIONS`.
   The Kroger API has no "all promos" endpoint, so the app searches for your frequent products
   (plus `KROGER_EXTRA_TERMS`) at each location and keeps the promo prices. Every price it sees goes into price history.
4. **Safeway, Metropolitan Market, Grocery Outlet**: set `SAFEWAY_AD_SOURCE`, `METMARKET_AD_SOURCE` and
   `GROCERY_OUTLET_AD_SOURCE` to the weekly ad URL or a downloaded PDF/image. Grocery Outlet ads show
   "compare at" prices rather than regular prices; those are used as the regular price. Claude extracts the deal lines. One-off: `grocery ingest --store safeway --file ad.pdf`.
   Manual fallback for any store: `grocery import-deals deals.csv` (see `examples/deals.csv`).
5. Run the week once by hand, then schedule it (`examples/crontab`):

```bash
grocery weekly --send     # receipts (last 10 days) → deals → match → cadence → digest → email
grocery review            # answer y/n for uncertain matches; answers are saved and reused
grocery top               # your top 50 items and how many have been matched (goal: 80%)
grocery savings           # what you saved per month
```

`grocery weekly` exits with status 2 when a configured store returned zero deals, and the digest
leads with an alert so a broken source is noticed the same morning.

## How it works

**Tables** (`src/grocery_deals/db.py`): `product` (canonical item), `store_item` (receipt name per store),
`purchase`, `deal`, `price_obs`, `product_cadence`, plus `alias` (saved mappings), `match_review`,
`match_reject` and `digest_item`.

**Matching** (`matching.py`), in order:
1. UPC exact match
2. Saved mappings: every review answer is stored and reused next week
3. Fuzzy score on brand, name tokens and size (size ranges like "7–8 oz" handled; a brand conflict caps the score)
   - ≥ 0.85: matched automatically
   - 0.60–0.85: Claude tie-break (`grocery match --llm`, on by default in `weekly`), otherwise the review queue
4. Receipt lines create a new canonical product unless they match an existing one at ≥ 0.90

**Ranking** (`scoring.py`): `score = frequency × discount vs your typical price × due-soon factor`
- frequency: purchases in the last 6 months (log-scaled, capped at 12)
- discount: sale price vs your median paid price (falls back to the ad's regular price); deals that
  are not cheaper than what you usually pay are dropped; a 90-day low gets a 10% boost
- timing: days since last purchase vs your usual gap; shelf-stable items can be stocked up early

Tier 2 of the digest lists possible matches waiting for review, and big discounts in categories you buy often.

**Savings** (`savings.py`): card/sale savings printed on receipts, plus digest picks you then bought
that ad week at that store.

## Configuration notes

- Storage is SQLite (`GROCERY_DB`), which the plan allows for the start. The SQL is plain, so moving to
  Postgres is mostly a connection change.
- Extraction and tie-breaks use the Claude API (`ANTHROPIC_API_KEY`, model `GROCERY_LLM_MODEL`,
  default `claude-opus-5-5`). Deal ingestion from the Kroger API, CSV imports, matching without `--llm`,
  and the digest all work without it.
- Email goes out over Gmail SMTP with an app password (`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `DIGEST_TO`).
- Scraping is weekly and for personal use only. The official Kroger API is preferred wherever it covers a store.

## Not in the MVP (later)

Digital coupon awareness, a shopping list split by cheapest store, a full PWA or Pi dashboard tile,
partner sharing, embedding-based matching, and dbt models for the transforms.

## Development

```bash
pip install -e '.[dev]'
pytest
```
