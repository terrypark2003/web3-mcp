"""Vercel serverless function: read-only arbitrage opportunities as JSON.

GET /api/opportunities -> {polymarket: [...], cross_venue: [...], ev: [...], meta}

This is MONITORING ONLY. It never holds a wallet key and never places an order
(no stage/confirm endpoints exist here) — execution stays in the local app.

By default it serves the bundled demo data so the deployment renders instantly.
Set the env var LIVE_SCAN=1 in the Vercel project to attempt a bounded live
scan (Polymarket + Kalshi public APIs); on any failure it falls back to demo
data and reports the error in ``meta`` instead of erroring the page.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Make the repo-root package importable from inside api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_arb.webapp import read_only_payload  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - Vercel's expected handler name
        live = os.environ.get("LIVE_SCAN", "").lower() in ("1", "true", "yes")
        payload = read_only_payload(live)  # never raises; falls back to demo
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Cache at the edge so bursts of viewers don't each trigger a live scan.
        self.send_header("Cache-Control", "s-maxage=30, stale-while-revalidate=60")
        self.end_headers()
        self.wfile.write(body)
