"""Thin HTTP client for Polymarket's public Gamma and CLOB APIs.

Read-only. No authentication, no keys, no order placement. The endpoints
used here are public; nothing in this module can move funds.

Network access to ``gamma-api.polymarket.com`` and ``clob.polymarket.com``
is required. In a sandboxed environment with an egress allowlist, both
hosts must be added before this client can connect.
"""

from __future__ import annotations

from typing import Iterable

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketClient:
    def __init__(
        self,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "polymarket-arb-scanner/0.1")

    def active_markets(self, page_size: int = 500, max_pages: int = 40) -> list[dict]:
        """Fetch open, tradeable markets from Gamma (paginated)."""
        markets: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            resp = self.session.get(
                f"{self.gamma_base}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": page_size,
                    "offset": offset,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            markets.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return markets

    def order_book(self, token_id: str) -> dict:
        """Fetch a single token's order book from the CLOB."""
        resp = self.session.get(
            f"{self.clob_base}/book",
            params={"token_id": token_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def order_books(self, token_ids: Iterable[str]) -> dict[str, dict]:
        """Batch-fetch order books; returns token_id -> raw book.

        Falls back to per-token requests if the batch endpoint is unavailable.
        """
        ids = [str(t) for t in token_ids]
        if not ids:
            return {}
        try:
            resp = self.session.post(
                f"{self.clob_base}/books",
                json=[{"token_id": t} for t in ids],
                timeout=self.timeout,
            )
            resp.raise_for_status()
            books = resp.json()
            out: dict[str, dict] = {}
            for book in books:
                key = book.get("asset_id") or book.get("token_id")
                if key is not None:
                    out[str(key)] = book
            if out:
                return out
        except requests.RequestException:
            pass

        # Fallback: one request per token.
        out = {}
        for token_id in ids:
            try:
                out[token_id] = self.order_book(token_id)
            except requests.RequestException:
                continue
        return out
