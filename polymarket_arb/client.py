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

    def active_markets(
        self,
        page_size: int = 100,
        max_pages: int = 200,
        extra_params: dict | None = None,
    ) -> list[dict]:
        """Fetch open, tradeable markets from Gamma (paginated).

        Gamma caps ``limit`` at 100 server-side, so we request 100 and advance
        the offset by however many rows actually came back, stopping only on an
        empty page. (Requesting 500 used to return 100 and trip a ``len < limit``
        early-break, so the scanner silently saw only the first 100 markets.)

        Gamma also 422s once the offset runs past its pagination ceiling
        (~2000); we stop with what we have instead of failing. ``extra_params``
        lets callers narrow server-side — e.g. ``end_date_min`` / ``end_date_max``
        to fetch only markets resolving in a window, which both sidesteps the
        offset ceiling and is far cheaper.
        """
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": page_size,
        }
        if extra_params:
            params.update(extra_params)
        markets: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            try:
                resp = self.session.get(
                    f"{self.gamma_base}/markets",
                    params={**params, "offset": offset},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                batch = resp.json()
            except requests.RequestException:
                break
            if not batch:
                break
            markets.extend(batch)
            offset += len(batch)
        return markets

    def active_events(self, page_size: int = 100, max_pages: int = 200) -> list[dict]:
        """Fetch open events (which group multi-candidate sub-markets) from Gamma.

        Same Gamma 100-row cap as ``active_markets`` — advance by the actual
        batch length and stop only on an empty page.
        """
        events: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            try:
                resp = self.session.get(
                    f"{self.gamma_base}/events",
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
            except requests.RequestException:
                break
            if not batch:
                break
            events.extend(batch)
            offset += len(batch)
        return events

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
