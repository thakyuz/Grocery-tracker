"""Build, render and send the weekly 'your items on sale' digest."""

from __future__ import annotations

import html
import json
import smtplib
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from .config import STORE_LABELS
from .savings import MonthSavings, monthly_savings
from .scoring import RankedDeal, rank_deals


@dataclass
class Digest:
    week_start: str
    items: list[RankedDeal]
    alerts: list[str] = field(default_factory=list)
    pending_reviews: int = 0
    deal_counts: dict[str, int] = field(default_factory=dict)
    savings: list[MonthSavings] = field(default_factory=list)

    @property
    def tier1(self) -> list[RankedDeal]:
        return [i for i in self.items if i.tier == 1]

    @property
    def tier2(self) -> list[RankedDeal]:
        return [i for i in self.items if i.tier == 2]

    @property
    def subject(self) -> str:
        n = len(self.tier1)
        total = sum(i.est_saving or 0 for i in self.tier1)
        warn = " ⚠" if self.alerts else ""
        return f"Grocery deals {self.week_start}: {n} of your items on sale (save ~${total:.2f}){warn}"


def build_digest(conn: sqlite3.Connection, week: str, expected_stores: list[str],
                 limit: int = 25, today: date | None = None) -> Digest:
    counts = {r["store"]: r["n"] for r in conn.execute(
        "SELECT store, COUNT(*) AS n FROM deal WHERE week_start=? GROUP BY store", (week,))}
    alerts = [f"No deals found for {STORE_LABELS.get(s, s)} this week - check its source."
              for s in expected_stores if not counts.get(s)]
    items = rank_deals(conn, week, today)
    tier1 = [i for i in items if i.tier == 1][:limit]
    tier2 = [i for i in items if i.tier == 2]
    pending = conn.execute("SELECT COUNT(*) FROM match_review WHERE status='pending'").fetchone()[0]

    conn.execute("DELETE FROM digest_item WHERE week_start=?", (week,))
    for i in tier1 + tier2:
        conn.execute(
            "INSERT INTO digest_item (week_start, deal_id, product_id, tier, score, est_saving) VALUES (?,?,?,?,?,?)",
            (week, i.deal_id, i.product_id, i.tier, i.score, i.est_saving))
    conn.commit()
    return Digest(week, tier1 + tier2, alerts, pending, counts, monthly_savings(conn, 3))


def _price(i: RankedDeal) -> str:
    unit = f"/{i.price_unit}" if i.price_unit and i.price_unit not in ("each", "ea") else ""
    return f"${i.sale_price:.2f}{unit}"


def _flags(i: RankedDeal) -> str:
    bits = []
    if i.is_90d_low:
        bits.append("90-day low")
    if i.coupon_required:
        bits.append("coupon")
    if i.deal_limit:
        bits.append(f"limit {i.deal_limit}")
    return ", ".join(bits)


def render_text(d: Digest) -> str:
    end = (date.fromisoformat(d.week_start) + timedelta(days=6)).isoformat()
    out = [f"Your items on sale, {d.week_start} to {end}", ""]
    for a in d.alerts:
        out.append(f"! {a}")
    if d.alerts:
        out.append("")
    if not d.tier1:
        out.append("None of your regular items are on sale this week.")
    for n, i in enumerate(d.tier1, 1):
        flags = f" [{_flags(i)}]" if _flags(i) else ""
        out.append(f"{n:>2}. {i.product_name} - {_price(i)} at {STORE_LABELS.get(i.store, i.store)} "
                   f"({i.discount:.0%} off your usual ${i.reference_price:.2f}){flags}")
        out.append(f"    {i.advice}. Ad: {i.raw_text}")
    if d.tier2:
        out += ["", "Also on sale in categories you buy:"]
        for i in d.tier2:
            out.append(f" - {i.raw_text} - {_price(i)} at {STORE_LABELS.get(i.store, i.store)} "
                       f"({i.discount:.0%} off; {i.advice})")
    if d.pending_reviews:
        out += ["", f"{d.pending_reviews} possible matches need a yes/no: run `grocery review`."]
    if d.savings:
        out += ["", "Savings by month:"]
        for s in d.savings:
            out.append(f" {s.month}: spent ${s.spent:.2f}, receipt savings ${s.receipt_savings:.2f}, "
                       f"{s.digest_hits} digest picks bought (~${s.digest_savings:.2f})")
    counts = ", ".join(f"{STORE_LABELS.get(k, k)} {v}" for k, v in sorted(d.deal_counts.items()))
    out += ["", f"Deals scanned: {counts or 'none'}"]
    return "\n".join(out)


def render_html(d: Digest) -> str:
    e = html.escape
    end = (date.fromisoformat(d.week_start) + timedelta(days=6)).isoformat()
    rows = []
    for n, i in enumerate(d.tier1, 1):
        flags = f'<div class="flags">{e(_flags(i))}</div>' if _flags(i) else ""
        rows.append(
            f"<tr><td class='n'>{n}</td><td><strong>{e(i.product_name or '')}</strong>"
            f"<div class='ad'>{e(i.raw_text)}</div>{flags}</td>"
            f"<td>{e(STORE_LABELS.get(i.store, i.store))}</td>"
            f"<td class='num'><strong>{e(_price(i))}</strong><div class='ad'>usual ${i.reference_price:.2f}</div></td>"
            f"<td class='num'>{i.discount:.0%}</td><td>{e(i.advice)}</td></tr>")
    tier2 = "".join(
        f"<li>{e(i.raw_text)} - <strong>{e(_price(i))}</strong> at {e(STORE_LABELS.get(i.store, i.store))} "
        f"({i.discount:.0%} off; {e(i.advice)})</li>" for i in d.tier2)
    alerts = "".join(f"<p class='alert'>{e(a)}</p>" for a in d.alerts)
    savings = "".join(
        f"<tr><td>{s.month}</td><td class='num'>${s.spent:.2f}</td><td class='num'>${s.receipt_savings:.2f}</td>"
        f"<td class='num'>{s.digest_hits}</td><td class='num'>${s.digest_savings:.2f}</td></tr>" for s in d.savings)
    counts = ", ".join(f"{STORE_LABELS.get(k, k)} {v}" for k, v in sorted(d.deal_counts.items()))
    review = (f"<p class='note'>{d.pending_reviews} possible matches need a yes/no: run "
              f"<code>grocery review</code>.</p>" if d.pending_reviews else "")
    table = (f"<table><thead><tr><th>#</th><th>Item</th><th>Store</th><th class='num'>Price</th>"
             f"<th class='num'>Off</th><th>Advice</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
             if rows else "<p>None of your regular items are on sale this week.</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grocery deals {d.week_start}</title>
<style>
 body {{ font: 15px/1.45 -apple-system, Segoe UI, Roboto, sans-serif; color: #1f2328; background: #fff;
        max-width: 820px; margin: 0 auto; padding: 16px; }}
 h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 28px 0 8px; }}
 .sub {{ color: #59636e; margin: 0 0 16px; }}
 table {{ width: 100%; border-collapse: collapse; }}
 th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
 th {{ font-size: 12px; text-transform: uppercase; color: #59636e; }}
 .num {{ text-align: right; white-space: nowrap; }} .n {{ color: #59636e; }}
 .ad {{ color: #59636e; font-size: 13px; }} .flags {{ color: #1a7f37; font-size: 13px; }}
 .alert {{ background: #fff8c5; border: 1px solid #d4a72c; padding: 8px 12px; border-radius: 6px; }}
 .note {{ color: #59636e; }} li {{ margin: 4px 0; }}
</style></head><body>
<h1>Your items on sale</h1>
<p class="sub">Ad week {d.week_start} to {end} · generated {datetime.now():%Y-%m-%d %H:%M}</p>
{alerts}{table}
{"<h2>Also on sale in categories you buy</h2><ul>" + tier2 + "</ul>" if tier2 else ""}
{review}
{"<h2>Savings by month</h2><table><thead><tr><th>Month</th><th class='num'>Spent</th><th class='num'>Receipt savings</th><th class='num'>Digest picks bought</th><th class='num'>Est. saved</th></tr></thead><tbody>" + savings + "</tbody></table>" if savings else ""}
<p class="note">Deals scanned: {e(counts or "none")}</p>
</body></html>"""


def write_digest(d: Digest, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"digest-{d.week_start}.html"
    path.write_text(render_html(d))
    return path


def send_digest(d: Digest, user: str, app_password: str, to: str,
                host: str = "smtp.gmail.com", port: int = 465) -> None:
    msg = EmailMessage()
    msg["Subject"] = d.subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(render_text(d))
    msg.add_alternative(render_html(d), subtype="html")
    with smtplib.SMTP_SSL(host, port) as smtp:
        smtp.login(user, app_password)
        smtp.send_message(msg)


def log_digest(conn, d: Digest, sent: bool) -> None:
    conn.execute(
        """INSERT INTO digest_log (week_start, sent_at, n_items, alerts) VALUES (?,?,?,?)
           ON CONFLICT (week_start) DO UPDATE SET sent_at=COALESCE(excluded.sent_at, digest_log.sent_at),
               n_items=excluded.n_items, alerts=excluded.alerts""",
        (d.week_start, datetime.now().isoformat(timespec="seconds") if sent else None,
         len(d.tier1), json.dumps(d.alerts)))
    conn.commit()
