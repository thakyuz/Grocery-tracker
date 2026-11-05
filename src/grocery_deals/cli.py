"""`grocery` command line."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from . import cadence, matching
from .config import STORE_LABELS, STORES, Settings
from .db import connect
from .digest import build_digest, log_digest, render_text, send_digest, write_digest
from .normalize import week_start

log = logging.getLogger("grocery")


def _week(args) -> str:
    return (date.fromisoformat(args.week) if getattr(args, "week", None) else week_start()).isoformat()


# ---- commands ---------------------------------------------------------------

def cmd_init(args, s, conn):
    print(f"Database ready at {s.db_path}")
    stores = s.configured_stores()
    print("Deal sources configured:", ", ".join(STORE_LABELS[x] for x in stores) or "none (see .env.example)")


def cmd_import_receipts(args, s, conn):
    from .receipts import import_file

    total = 0
    for p in args.paths:
        paths = sorted(Path(p).iterdir()) if Path(p).is_dir() else [Path(p)]
        for path in paths:
            if path.is_file():
                n = import_file(conn, path, s.llm_model, args.store)
                print(f"{path}: {n} new purchase lines")
                total += n
    cadence.rebuild_cadence(conn)
    print(f"Imported {total} purchase lines")


def cmd_fetch_gmail(args, s, conn):
    from .receipts import fetch_gmail

    if not (s.gmail_user and s.gmail_app_password):
        sys.exit("Set GMAIL_USER and GMAIL_APP_PASSWORD")
    stats = fetch_gmail(conn, s.gmail_user, s.gmail_app_password, s.llm_model, args.days)
    cadence.rebuild_cadence(conn)
    print(f"Imported {stats['lines']} lines from {stats['emails']} receipt emails ({stats['errors']} errors)")


def ingest_all(s: Settings, conn, week: date, only: str | None = None, file: str | None = None) -> dict[str, int]:
    """Pull this week's deals from every configured source. Failures are logged, not fatal."""
    counts: dict[str, int] = {}
    kroger_stores = {k: v for k, v in s.kroger_locations.items() if not only or k == only}
    if kroger_stores and not file:
        if s.kroger_client_id and s.kroger_client_secret:
            from .sources import kroger

            client = kroger.KrogerClient(s.kroger_client_id, s.kroger_client_secret)
            try:
                counts.update(kroger.ingest(conn, client, kroger_stores, week, s.kroger_extra_terms))
            except Exception as e:
                log.error("Kroger ingest failed: %s", e)
        elif only:
            log.error("Set KROGER_CLIENT_ID / KROGER_CLIENT_SECRET for %s", only)
    ad_sources = dict(s.ad_sources)
    if file:
        if not only:
            sys.exit("--file needs --store")
        ad_sources = {only: file}
    for store, source in ad_sources.items():
        if only and store != only:
            continue
        from .sources import flyer

        try:
            counts[store] = flyer.ingest(conn, store, source, week, s.llm_model)
        except Exception as e:
            log.error("%s ad extraction failed: %s", store, e)
            counts[store] = 0
    return counts


def cmd_ingest(args, s, conn):
    week = date.fromisoformat(_week(args))
    counts = ingest_all(s, conn, week, args.store, args.file)
    for store, n in counts.items():
        print(f"{STORE_LABELS.get(store, store)}: {n} deals for week of {week}")
    if not counts:
        print("No deal sources configured; see .env.example or use `grocery import-deals`.")


def cmd_import_deals(args, s, conn):
    from .sources import csv_deals

    n = csv_deals.ingest(conn, args.path, date.fromisoformat(_week(args)), args.store)
    print(f"Imported {n} deals for week of {_week(args)}")


def cmd_match(args, s, conn):
    stats = matching.match_deals(conn, None if args.all_weeks else _week(args), args.llm, s.llm_model)
    print("Matched: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    if stats["review"]:
        print("Run `grocery review` to confirm the uncertain ones.")


def cmd_review(args, s, conn):
    rows = matching.pending_reviews(conn)
    if not rows:
        print("Nothing to review.")
        return
    print("For each: y = same product, n = not a match, s = skip, q = quit\n")
    for r in rows:
        print(f"[{STORE_LABELS.get(r['store'], r['store'])}] ad: {r['raw_text']}  (${r['sale_price']:.2f})")
        print(f"    you buy: {matching.describe_product(r)}   score {r['score']:.2f}")
        ans = input("    match? [y/n/s/q] ").strip().lower()
        if ans == "q":
            break
        if ans in ("y", "n"):
            matching.resolve_review(conn, r["review_id"], ans == "y")


def cmd_set_match(args, s, conn):
    matching.manual_match(conn, args.deal_id, args.product_id)
    print(f"Deal {args.deal_id} -> product {args.product_id} (saved for future weeks)")


def cmd_cadence(args, s, conn):
    n = cadence.rebuild_cadence(conn)
    print(f"Cadence rebuilt for {n} products")


def cmd_top(args, s, conn):
    cadence.rebuild_cadence(conn)
    week = _week(args)
    matched = {r["product_id"] for r in conn.execute(
        "SELECT DISTINCT product_id FROM deal WHERE product_id IS NOT NULL")}
    rows = cadence.top_products(conn, args.limit)
    print(f"{'id':>4}  {'buys/6mo':>8}  {'every':>7}  {'last':>10}  {'usual $':>7}  seen in ads  product")
    for r in rows:
        every = f"{r['avg_days_between']:.0f}d" if r["avg_days_between"] else "-"
        price = f"{r['typical_price']:.2f}" if r["typical_price"] else "-"
        print(f"{r['product_id']:>4}  {r['purchases_6mo']:>8}  {every:>7}  {r['last_bought']:>10}  {price:>7}  "
              f"{'yes' if r['product_id'] in matched else '   '}          {matching.describe_product(r)}")
    if rows:
        pct = sum(1 for r in rows if r["product_id"] in matched) / len(rows)
        print(f"\n{pct:.0%} of your top {len(rows)} items have been matched to a store deal at least once "
              f"(target 80%). Current week: {week}")


def cmd_deals(args, s, conn):
    week = _week(args)
    rows = conn.execute(
        """SELECT d.*, p.name AS pname, p.brand AS pbrand FROM deal d LEFT JOIN product p USING (product_id)
           WHERE week_start=? ORDER BY store, raw_text""", (week,)).fetchall()
    for r in rows:
        if args.matched and not r["product_id"]:
            continue
        m = f" -> {r['pbrand'] + ' ' if r['pbrand'] else ''}{r['pname']} ({r['match_method']})" if r["product_id"] else ""
        reg = f" (reg ${r['regular_price']:.2f})" if r["regular_price"] else ""
        print(f"{r['deal_id']:>5} {STORE_LABELS.get(r['store'], r['store']):<20} ${r['sale_price']:>6.2f}{reg}  "
              f"{r['raw_text']}{m}")
    print(f"{len(rows)} deals for week of {week}")


def cmd_products(args, s, conn):
    for r in conn.execute("SELECT * FROM product ORDER BY product_id"):
        print(f"{r['product_id']:>4}  {matching.describe_product(r)}  [{r['category'] or '-'}]")


def run_digest(s: Settings, conn, week: str, send: bool, show: bool = True):
    d = build_digest(conn, week, s.configured_stores())
    path = write_digest(d, s.out_dir)
    if show:
        print(render_text(d))
        print(f"\nHTML: {path}")
    if send:
        if not (s.gmail_user and s.gmail_app_password and s.digest_to):
            sys.exit("Set GMAIL_USER, GMAIL_APP_PASSWORD and DIGEST_TO to send")
        send_digest(d, s.gmail_user, s.gmail_app_password, s.digest_to)
        print(f"Sent to {s.digest_to}")
    log_digest(conn, d, send)
    return d


def cmd_digest(args, s, conn):
    cadence.rebuild_cadence(conn)
    run_digest(s, conn, _week(args), args.send)


def cmd_weekly(args, s, conn):
    """The Wednesday job: deals -> match -> cadence -> digest -> email."""
    week = week_start()
    if args.gmail_days and s.gmail_user and s.gmail_app_password:
        from .receipts import fetch_gmail

        try:
            stats = fetch_gmail(conn, s.gmail_user, s.gmail_app_password, s.llm_model, args.gmail_days)
            print(f"Receipts: {stats['lines']} new lines")
        except Exception as e:
            log.error("Gmail receipt fetch failed: %s", e)
    counts = ingest_all(s, conn, week)
    print("Deals:", ", ".join(f"{STORE_LABELS.get(k, k)} {v}" for k, v in counts.items()) or "none")
    cadence.rebuild_cadence(conn)
    stats = matching.match_deals(conn, week.isoformat(), use_llm=not args.no_llm, model=s.llm_model)
    print("Matched:", ", ".join(f"{k} {v}" for k, v in stats.items()))
    d = run_digest(s, conn, week.isoformat(), args.send, show=args.verbose)
    print(d.subject)
    if d.alerts:
        for a in d.alerts:
            log.warning(a)
        sys.exit(2)


def cmd_savings(args, s, conn):
    from .savings import monthly_savings

    for m in monthly_savings(conn, args.months):
        print(f"{m.month}: spent ${m.spent:.2f} | receipt savings ${m.receipt_savings:.2f} | "
              f"{m.digest_hits} digest picks bought (~${m.digest_savings:.2f})")


def cmd_kroger_locations(args, s, conn):
    from .sources.kroger import KrogerClient

    if not (s.kroger_client_id and s.kroger_client_secret):
        sys.exit("Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET")
    for loc in KrogerClient(s.kroger_client_id, s.kroger_client_secret).locations(args.zip):
        addr = loc.get("address", {})
        print(f"{loc.get('locationId')}  {loc.get('chain', ''):<10} {loc.get('name', '')} - "
              f"{addr.get('addressLine1', '')}, {addr.get('city', '')}")


def cmd_serve(args, s, conn):
    from .web import serve

    serve(s, args.host, args.port)


def cmd_demo(args, s, conn):
    from .demo import seed

    seed(conn)
    cadence.rebuild_cadence(conn)
    week = week_start().isoformat()
    stats = matching.match_deals(conn, week)
    print("Matched:", ", ".join(f"{k} {v}" for k, v in stats.items()), "\n")
    run_digest(s, conn, week, send=False)


# ---- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grocery", description="Weekly digest of deals on the groceries you buy.")
    p.add_argument("--db", help="SQLite path (default: $GROCERY_DB or data/grocery.db)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "Create the database and show configured sources")

    sp = add("import-receipts", cmd_import_receipts,
             "Import purchase history: order-history CSVs, receipt photos/PDFs, .eml files, or folders of them")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--store", choices=STORES, help="Store, if the files don't say")

    sp = add("fetch-gmail", cmd_fetch_gmail, "Import digital receipts from Gmail over IMAP")
    sp.add_argument("--days", type=int, default=30)

    sp = add("ingest", cmd_ingest, "Pull this week's deals from the configured sources")
    sp.add_argument("--store", choices=STORES)
    sp.add_argument("--file", help="Ad file or URL to extract instead of the configured source (needs --store)")
    sp.add_argument("--week", help="Ad week start (YYYY-MM-DD, a Wednesday); default this week")

    sp = add("import-deals", cmd_import_deals, "Import deals from a CSV (manual fallback)")
    sp.add_argument("path")
    sp.add_argument("--store", choices=STORES)
    sp.add_argument("--week")

    sp = add("match", cmd_match, "Match deals to the products you buy")
    sp.add_argument("--llm", action="store_true", help="Use Claude to settle uncertain matches")
    sp.add_argument("--week")
    sp.add_argument("--all-weeks", action="store_true")

    add("review", cmd_review, "Confirm or reject uncertain matches")

    sp = add("set-match", cmd_set_match, "Manually map a deal to a product (saved for future weeks)")
    sp.add_argument("deal_id", type=int)
    sp.add_argument("product_id", type=int)

    add("cadence", cmd_cadence, "Rebuild buying cadence per product")

    sp = add("top", cmd_top, "Your most-bought items and their match coverage")
    sp.add_argument("--limit", type=int, default=50)

    sp = add("deals", cmd_deals, "List this week's deals")
    sp.add_argument("--week")
    sp.add_argument("--matched", action="store_true")

    add("products", cmd_products, "List canonical products")

    sp = add("digest", cmd_digest, "Build (and optionally email) the weekly digest")
    sp.add_argument("--week")
    sp.add_argument("--send", action="store_true")

    sp = add("weekly", cmd_weekly, "The scheduled job: receipts, deals, matching, digest, email")
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--no-llm", action="store_true", help="Skip LLM match tie-breaks")
    sp.add_argument("--gmail-days", type=int, default=10, help="Fetch receipts from the last N days (0 = skip)")

    sp = add("savings", cmd_savings, "What you saved per month")
    sp.add_argument("--months", type=int, default=6)

    sp = add("kroger-locations", cmd_kroger_locations, "Find Fred Meyer / QFC location IDs near a ZIP")
    sp.add_argument("--zip", required=True)

    sp = add("serve", cmd_serve, "Serve a basic web page with this week's digest and deals")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)

    add("demo", cmd_demo, "Seed sample data into the database and print a digest (try with --db out/demo.db)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    if args.db:
        s.db_path = args.db
    conn = connect(s.db_path)
    try:
        args.fn(args, s, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
