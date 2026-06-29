"""Load the bundled synthetic complete sets for the offline demo."""

from __future__ import annotations

import json
import os
from typing import Optional

from .crossvenue import MatchedMarket
from .ev import FairValue, fair_value_from_map
from .kalshi_normalize import complete_set_from_kalshi
from .models import CompleteSet, Leg, Level

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "demo_sets.json")
_CROSS_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "demo_cross_venue.json"
)
_WORLD_CUP_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "demo_world_cup.json"
)
_WORLD_CUP_MATCHES_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "demo_world_cup_matches.json"
)


def _level(raw: Optional[list]) -> Optional[Level]:
    if not raw:
        return None
    price, size = raw
    return Level(price=float(price), size=float(size))


def load_demo_sets(path: str = _FIXTURE) -> list[CompleteSet]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    sets: list[CompleteSet] = []
    for entry in data.get("sets", []):
        legs = [
            Leg(
                token_id=leg["token_id"],
                outcome=leg["outcome"],
                best_ask=_level(leg.get("ask")),
                best_bid=_level(leg.get("bid")),
            )
            for leg in entry["legs"]
        ]
        sets.append(
            CompleteSet(
                market_id=entry["market_id"],
                question=entry["question"],
                legs=legs,
                neg_risk=entry.get("neg_risk", False),
                exhaustive=entry.get("exhaustive", True),
                end_date=entry.get("end_date"),
            )
        )
    return sets


def _poly_set_from_entry(entry: dict) -> CompleteSet:
    legs = [
        Leg(
            token_id=leg["token_id"],
            outcome=leg["outcome"],
            best_ask=_level(leg.get("ask")),
            best_bid=_level(leg.get("bid")),
            venue="polymarket",
        )
        for leg in entry["legs"]
    ]
    return CompleteSet(
        market_id=entry["market_id"],
        question=entry["question"],
        legs=legs,
        end_date=entry.get("end_date"),
        venue="polymarket",
        url=entry.get("url"),
    )


def _load_cross_fixture(path: str = _CROSS_FIXTURE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_demo_cross_venue(path: str = _CROSS_FIXTURE) -> list[MatchedMarket]:
    """Build matched Kalshi/Polymarket events from the offline demo fixture."""
    from .matching import Pair, build_matched_markets

    data = _load_cross_fixture(path)
    kalshi_by_ticker = {
        cs.market_id: cs
        for cs in (
            complete_set_from_kalshi(k["market"], k.get("orderbook"))
            for k in data.get("kalshi", [])
        )
        if cs is not None
    }
    poly_by_id = {
        e["market_id"]: _poly_set_from_entry(e) for e in data.get("polymarket", [])
    }
    pairs = [
        Pair(
            event_id=p["event_id"],
            question=p["question"],
            kalshi_ticker=p["kalshi_ticker"],
            polymarket_market_id=p["polymarket_market_id"],
            end_date=p.get("end_date"),
        )
        for p in data.get("pairs", [])
    ]
    return build_matched_markets(pairs, kalshi_by_ticker, poly_by_id)


def load_demo_ev_sets(path: str = _CROSS_FIXTURE) -> tuple[list[CompleteSet], FairValue]:
    """All demo binary sets (both venues) plus the fair-value source for EV."""
    data = _load_cross_fixture(path)
    sets: list[CompleteSet] = [
        cs
        for cs in (
            complete_set_from_kalshi(k["market"], k.get("orderbook"))
            for k in data.get("kalshi", [])
        )
        if cs is not None
    ]
    sets.extend(_poly_set_from_entry(e) for e in data.get("polymarket", []))
    fair = fair_value_from_map(data.get("fair_values", {}))
    return sets, fair


def load_demo_world_cup(path: str = _WORLD_CUP_FIXTURE, min_edge: float = 0.02):
    """Build World Cup outright value opportunities from the offline demo fixture."""
    from .sports_value import scan_world_cup_value

    data = _load_cross_fixture(path)
    sets = [_poly_set_from_entry(e) for e in data.get("polymarket", [])]
    prices = data.get("bookmaker_prices", {})
    return scan_world_cup_value(sets, prices, min_edge=min_edge)


def load_demo_world_cup_matches(
    path: str = _WORLD_CUP_MATCHES_FIXTURE, min_edge: float = 0.05
):
    """Build per-MATCH World Cup value opportunities from the offline demo fixture."""
    from .sports_value import scan_world_cup_match_value

    data = _load_cross_fixture(path)
    sets = [_poly_set_from_entry(e) for e in data.get("polymarket", [])]
    matches = data.get("matches", [])
    return scan_world_cup_match_value(sets, matches, min_edge=min_edge)
