"""Read-only client for The Odds API (bookmaker odds for the consensus).

Free tier: ~500 requests/month, so call it on a modest schedule (the World Cup
alert workflow defaults to a few hours, not every 15 min). Get a key at
https://the-odds-api.com/ and pass it via the ODDS_API_KEY env var.

Network access to ``api.the-odds-api.com`` is required. Written to the
documented v4 interface but UNRUN here (no key/egress) — validate against your
key before trusting it.

Response shape (``/sports/{sport}/odds``):
    [ { "bookmakers": [ { "markets": [
          { "key": "outrights", "outcomes": [ {"name":"Brazil","price":5.5}, ... ] }
        ] } ] }, ... ]
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
WORLD_CUP_WINNER = "soccer_fifa_world_cup_winner"   # outright tournament winner
WORLD_CUP_MATCHES = "soccer_fifa_world_cup"         # individual matches (h2h)


class OddsApiClient:
    def __init__(
        self,
        api_key: str,
        base: str = ODDS_API_BASE,
        session: Optional[requests.Session] = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _odds(self, sport_key: str, markets: str, regions: str = "us,uk,eu") -> list:
        resp = self.session.get(
            f"{self.base}/sports/{sport_key}/odds",
            params={
                "apiKey": self.api_key,
                "regions": regions,
                "oddsFormat": "decimal",
                "markets": markets,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def world_cup_winner_prices(self) -> dict[str, list[float]]:
        """Return {team: [decimal_odds across bookmakers]} for the outright winner."""
        events = self._odds(WORLD_CUP_WINNER, markets="outrights")
        return prices_by_team_from_events(events, market_key="outrights")

    def world_cup_match_odds(self) -> list[dict]:
        """Per-match head-to-head odds for upcoming/live World Cup games.

        Returns one dict per match::

            {"home": "Argentina", "away": "Mexico",
             "commence_time": "2026-06-30T18:00:00Z",
             "prices": {"Argentina": [1.8, ...], "Draw": [3.6, ...], "Mexico": [...]}}

        Unlike the outright winner, these markets settle right after the match —
        the near-dated value the alerts are meant to surface.
        """
        events = self._odds(WORLD_CUP_MATCHES, markets="h2h")
        return match_prices_from_events(events, market_key="h2h")


def prices_by_team_from_events(events: list, market_key: str) -> dict[str, list[float]]:
    """Flatten The Odds API events into {outcome_name: [decimal prices]}.

    Pure parser (tested offline against captured fixtures).
    """
    prices: dict[str, list[float]] = defaultdict(list)
    for event in events or []:
        for book in event.get("bookmakers", []) or []:
            for market in book.get("markets", []) or []:
                if market_key and market.get("key") != market_key:
                    continue
                for outcome in market.get("outcomes", []) or []:
                    name = outcome.get("name")
                    price = outcome.get("price")
                    if name is None or price is None:
                        continue
                    try:
                        prices[str(name)].append(float(price))
                    except (TypeError, ValueError):
                        continue
    return dict(prices)


def match_prices_from_events(events: list, market_key: str = "h2h") -> list[dict]:
    """Flatten The Odds API h2h events into per-match price tables.

    Pure parser (tested offline against captured fixtures). One entry per event,
    each with ``home``/``away``/``commence_time`` and a ``prices`` map of
    {outcome_name: [decimal prices across books]} (outcome is a team or "Draw").
    """
    out: list[dict] = []
    for event in events or []:
        prices: dict[str, list[float]] = defaultdict(list)
        for book in event.get("bookmakers", []) or []:
            for market in book.get("markets", []) or []:
                if market_key and market.get("key") != market_key:
                    continue
                for outcome in market.get("outcomes", []) or []:
                    name = outcome.get("name")
                    price = outcome.get("price")
                    if name is None or price is None:
                        continue
                    try:
                        prices[str(name)].append(float(price))
                    except (TypeError, ValueError):
                        continue
        if prices:
            out.append({
                "home": event.get("home_team"),
                "away": event.get("away_team"),
                "commence_time": event.get("commence_time"),
                "prices": dict(prices),
            })
    return out
