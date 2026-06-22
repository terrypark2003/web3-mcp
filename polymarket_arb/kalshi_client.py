"""Thin, read-only HTTP client for Kalshi's public market-data API.

Read-only. No authentication, no keys, no order placement — the endpoints used
here are public market data. (Trading on Kalshi *does* require an authenticated
session; that lives in the execution layer, not here.)

Network access to ``api.elections.kalshi.com`` is required. In a sandboxed
environment with an egress allowlist, that host must be added first.

Endpoints (see kalshi_normalize.py for the response-shape assumptions):
    GET /trade-api/v2/markets?status=open
    GET /trade-api/v2/markets/{ticker}
    GET /trade-api/v2/markets/{ticker}/orderbook
"""

from __future__ import annotations

from typing import Iterable, Optional

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(
        self,
        base: str = KALSHI_BASE,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "prediction-arb-scanner/0.1")

    def market(self, ticker: str) -> Optional[dict]:
        """Fetch one market's metadata; returns the inner ``market`` object."""
        resp = self.session.get(
            f"{self.base}/markets/{ticker}", timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json().get("market")

    def orderbook(self, ticker: str, depth: int = 1) -> dict:
        """Fetch one market's order book (raw, as returned by Kalshi)."""
        resp = self.session.get(
            f"{self.base}/markets/{ticker}/orderbook",
            params={"depth": depth},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def markets_by_ticker(
        self, tickers: Iterable[str], depth: int = 1
    ) -> dict[str, dict]:
        """Fetch ``{ticker: {"market": ..., "orderbook": ...}}`` for each ticker.

        Tickers that error (closed, renamed, network) are skipped rather than
        aborting the whole scan.
        """
        out: dict[str, dict] = {}
        for ticker in tickers:
            try:
                market = self.market(ticker)
                if market is None:
                    continue
                book = self.orderbook(ticker, depth=depth)
            except requests.RequestException:
                continue
            out[str(ticker)] = {"market": market, "orderbook": book}
        return out

    def active_markets(self, page_size: int = 200, max_pages: int = 40) -> list[dict]:
        """Fetch open markets (paginated via cursor). Metadata only, no books."""
        markets: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params = {"status": "open", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            resp = self.session.get(
                f"{self.base}/markets", params=params, timeout=self.timeout
            )
            resp.raise_for_status()
            payload = resp.json()
            batch = payload.get("markets") or []
            markets.extend(batch)
            cursor = payload.get("cursor")
            if not cursor or not batch:
                break
        return markets
