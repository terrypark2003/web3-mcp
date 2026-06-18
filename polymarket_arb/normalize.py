"""Translate raw Polymarket API JSON into normalized models.

This is the ONLY module that encodes assumptions about Polymarket's
response shapes, so if the API changes, the fix is contained here.

Assumptions (documented so they are easy to verify against a live snapshot):

* Gamma ``/markets`` returns objects with:
    - ``id``                market id
    - ``question``          human-readable question
    - ``endDate``           ISO-8601 resolution time
    - ``negRisk``           bool, true for negative-risk (multi-candidate) markets
    - ``clobTokenIds``      JSON-encoded string list of token ids, aligned with ``outcomes``
    - ``outcomes``          JSON-encoded string list of outcome labels, e.g. '["Yes","No"]'
    - ``outcomePrices``     JSON-encoded string list of *indicative* prices (mid/last)

* CLOB ``/book`` (per token) returns:
    - ``asks``  list of {"price": "0.61", "size": "120"}  (sellers; we BUY from these)
    - ``bids``  list of {"price": "0.59", "size": "150"}  (buyers; we SELL into these)

``outcomePrices`` are indicative only and must never be used as executable
prices for an arbitrage decision — always confirm against the live order
book. They are useful purely as a cheap pre-filter.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .models import CompleteSet, Leg, Level


def _maybe_json_list(value: Any) -> list:
    """Gamma encodes several list fields as JSON strings; decode defensively."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def top_of_book(raw_book: Optional[dict]) -> tuple[Optional[Level], Optional[Level]]:
    """Return (best_ask, best_bid) from a raw CLOB ``/book`` response.

    best_ask = lowest-priced ask (cheapest share to buy).
    best_bid = highest-priced bid (best price to sell into).
    """
    if not raw_book:
        return None, None

    best_ask = None
    for entry in raw_book.get("asks", []) or []:
        price = _to_float(entry.get("price"))
        size = _to_float(entry.get("size"))
        if price is None or size is None or size <= 0:
            continue
        if best_ask is None or price < best_ask.price:
            best_ask = Level(price=price, size=size)

    best_bid = None
    for entry in raw_book.get("bids", []) or []:
        price = _to_float(entry.get("price"))
        size = _to_float(entry.get("size"))
        if price is None or size is None or size <= 0:
            continue
        if best_bid is None or price > best_bid.price:
            best_bid = Level(price=price, size=size)

    return best_ask, best_bid


def complete_set_from_market(
    market: dict, books_by_token: dict[str, dict]
) -> Optional[CompleteSet]:
    """Build a CompleteSet from one Gamma market + the order books of its tokens.

    Works for binary markets (2 outcomes). Each outcome token has its own
    order book; ``books_by_token`` maps token_id -> raw ``/book`` response.
    """
    token_ids = [str(t) for t in _maybe_json_list(market.get("clobTokenIds"))]
    outcomes = [str(o) for o in _maybe_json_list(market.get("outcomes"))]
    if len(token_ids) < 2 or len(token_ids) != len(outcomes):
        return None

    legs: list[Leg] = []
    for token_id, outcome in zip(token_ids, outcomes):
        best_ask, best_bid = top_of_book(books_by_token.get(token_id))
        legs.append(
            Leg(token_id=token_id, outcome=outcome, best_ask=best_ask, best_bid=best_bid)
        )

    return CompleteSet(
        market_id=str(market.get("id", "")),
        question=str(market.get("question", "")),
        legs=legs,
        neg_risk=bool(market.get("negRisk", False)),
        exhaustive=True,  # a single binary market is exhaustive by construction
        end_date=market.get("endDate"),
    )


def indicative_set_cost(market: dict) -> Optional[float]:
    """Sum of indicative ``outcomePrices`` — a cheap pre-filter, not executable."""
    prices = [_to_float(p) for p in _maybe_json_list(market.get("outcomePrices"))]
    prices = [p for p in prices if p is not None]
    if not prices:
        return None
    return sum(prices)
