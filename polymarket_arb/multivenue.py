"""Live orchestration for cross-venue arbitrage and positive-EV scanning.

Network-touching glue that sits above the pure detectors:

    cross-venue:  load curated pairs -> fetch each venue -> match -> detect
    positive-EV:  fetch a venue's binary markets -> apply a fair-value source

The Polymarket and Kalshi clients are the only things here that hit the
network; everything they return flows through the same normalize layer the
offline demo uses, so live and demo paths share one code path for detection.
"""

from __future__ import annotations

from typing import Optional

from .crossvenue import CrossVenueOpportunity, scan_cross_venue
from .ev import EVOpportunity, FairValue, scan_ev
from .kalshi_normalize import complete_set_from_kalshi
from .matching import Pair, build_matched_markets, load_pairs
from .models import CompleteSet
from .normalize import _maybe_json_list, complete_set_from_market
from .venues import VenueFee, default_venue_fees


def _kalshi_sets(kalshi_client, tickers) -> dict[str, CompleteSet]:
    raw = kalshi_client.markets_by_ticker(tickers)
    out: dict[str, CompleteSet] = {}
    for ticker, payload in raw.items():
        cs = complete_set_from_kalshi(payload["market"], payload.get("orderbook"))
        if cs is not None:
            out[ticker] = cs
    return out


def _poly_sets_by_id(poly_client, wanted_ids: set[str]) -> dict[str, CompleteSet]:
    """Fetch active Polymarket markets, keep the wanted ids, attach order books."""
    markets = [
        m for m in poly_client.active_markets() if str(m.get("id", "")) in wanted_ids
    ]
    token_ids: list[str] = []
    for market in markets:
        token_ids.extend(str(t) for t in _maybe_json_list(market.get("clobTokenIds")))
    books = poly_client.order_books(token_ids)
    out: dict[str, CompleteSet] = {}
    for market in markets:
        cs = complete_set_from_market(market, books)
        if cs is not None:
            out[str(market.get("id", ""))] = cs
    return out


def scan_cross_venue_live(
    kalshi_client,
    poly_client,
    pairs: Optional[list[Pair]] = None,
    fees: Optional[dict[str, VenueFee]] = None,
    min_edge_per_set: float = 0.005,
    min_size: float = 1.0,
) -> list[CrossVenueOpportunity]:
    """Scan the curated cross-venue registry against live books on both venues."""
    pairs = pairs if pairs is not None else load_pairs()
    if not pairs:
        return []
    fees = fees or default_venue_fees()

    kalshi_by_ticker = _kalshi_sets(kalshi_client, [p.kalshi_ticker for p in pairs])
    poly_by_id = _poly_sets_by_id(
        poly_client, {p.polymarket_market_id for p in pairs}
    )
    matched = build_matched_markets(pairs, kalshi_by_ticker, poly_by_id)
    return scan_cross_venue(matched, fees, min_edge_per_set, min_size)


def scan_world_cup_value_live(
    poly_client,
    odds_client,
    min_edge: float = 0.03,
    min_size: float = 1.0,
) -> list[EVOpportunity]:
    """Live World Cup value: Polymarket outright markets vs bookmaker consensus.

    Fetches only World Cup markets (by question keyword) and their books, plus
    the bookmaker outright odds, then runs the consensus value detector.
    """
    from .normalize import complete_set_from_market
    from .sports_value import is_world_cup_market, scan_world_cup_value

    markets = [
        m for m in poly_client.active_markets()
        if is_world_cup_market(str(m.get("question", "")))
    ]
    token_ids: list[str] = []
    for market in markets:
        token_ids.extend(str(t) for t in _maybe_json_list(market.get("clobTokenIds")))
    books = poly_client.order_books(token_ids)
    sets = [
        cs for cs in (complete_set_from_market(m, books) for m in markets)
        if cs is not None
    ]

    prices = odds_client.world_cup_winner_prices()
    return scan_world_cup_value(sets, prices, min_edge=min_edge, min_size=min_size)


def scan_world_cup_match_value_live(
    poly_client,
    odds_client,
    min_edge: float = 0.05,
    min_size: float = 1.0,
) -> list[EVOpportunity]:
    """Live per-match World Cup value: Polymarket match markets vs h2h consensus.

    Fetches the upcoming/live match odds first, then keeps only Polymarket
    markets whose question names two of the teams playing (a match market) and
    attaches their books. These markets settle right after the match, so they
    are the near-dated value the alerts target.
    """
    from .sports_value import (
        _team_in_question,
        normalize_team,
        scan_world_cup_match_value,
    )

    matches = odds_client.world_cup_match_odds()
    if not matches:
        return []

    teams = set()
    for m in matches:
        for side in (m.get("home"), m.get("away")):
            if side:
                teams.add(normalize_team(side))

    candidates = []
    for market in poly_client.active_markets():
        q = str(market.get("question", "")).lower()
        if sum(1 for t in teams if _team_in_question(q, t)) >= 2:
            candidates.append(market)

    token_ids: list[str] = []
    for market in candidates:
        token_ids.extend(str(t) for t in _maybe_json_list(market.get("clobTokenIds")))
    books = poly_client.order_books(token_ids)
    sets = [
        cs for cs in (complete_set_from_market(m, books) for m in candidates)
        if cs is not None
    ]
    return scan_world_cup_match_value(sets, matches, min_edge=min_edge, min_size=min_size)


def scan_ev_live(
    poly_client,
    fair: FairValue,
    fees: Optional[dict[str, VenueFee]] = None,
    min_ev: float = 0.02,
    min_size: float = 1.0,
    limit: Optional[int] = None,
) -> list[EVOpportunity]:
    """Scan live Polymarket binary markets for positive EV against ``fair``.

    Only binary (2-leg) markets are considered; the fair-value source decides
    which ones are even evaluated (it returns None for unknowns).
    """
    from .scanner import build_sets_live
    from .detect import FeeModel

    sets = build_sets_live(poly_client, FeeModel(), limit=limit)
    binary = [cs for cs in sets if len(cs.legs) == 2]
    return scan_ev(binary, fair, fees or default_venue_fees(), min_ev, min_size)
