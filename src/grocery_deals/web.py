"""Basic read-only web page: this week's digest and every deal. Stdlib only."""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import cadence
from .config import STORE_LABELS, Settings
from .db import connect
from .digest import build_digest, render_html
from .normalize import week_start


def deals_page(conn, week: str) -> str:
    e = html.escape
    rows = conn.execute(
        """SELECT d.*, p.name AS pname, p.brand AS pbrand FROM deal d LEFT JOIN product p USING (product_id)
           WHERE week_start=? ORDER BY d.product_id IS NULL, store, raw_text""", (week,)).fetchall()
    body = "".join(
        f"<tr><td>{e(STORE_LABELS.get(r['store'], r['store']))}</td><td>{e(r['raw_text'])}</td>"
        f"<td class='num'>${r['sale_price']:.2f}</td>"
        f"<td class='num'>{'$%.2f' % r['regular_price'] if r['regular_price'] else ''}</td>"
        f"<td>{e(((r['pbrand'] or '') + ' ' + r['pname']).strip()) if r['pname'] else ''}</td></tr>"
        for r in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>All deals {week}</title>
<style>body{{font:15px/1.45 system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;background:#fff;color:#1f2328}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:6px;border-bottom:1px solid #e5e7eb}}
.num{{text-align:right}}</style></head><body>
<p><a href="/">&larr; Digest</a></p><h1>All deals, week of {week}</h1>
<table><thead><tr><th>Store</th><th>Ad</th><th class="num">Sale</th><th class="num">Regular</th>
<th>Matched to</th></tr></thead><tbody>{body}</tbody></table></body></html>"""


def serve(s: Settings, host: str = "127.0.0.1", port: int = 8000) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            week = parse_qs(url.query).get("week", [week_start().isoformat()])[0]
            conn = connect(s.db_path)
            try:
                if url.path == "/":
                    cadence.rebuild_cadence(conn)
                    page = render_html(build_digest(conn, week, s.configured_stores())).replace(
                        "<h1>", f'<p><a href="/deals?week={week}">All deals &rarr;</a></p><h1>', 1)
                elif url.path == "/deals":
                    page = deals_page(conn, week)
                else:
                    self.send_error(404)
                    return
            finally:
                conn.close()
            data = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    print(f"Serving on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
