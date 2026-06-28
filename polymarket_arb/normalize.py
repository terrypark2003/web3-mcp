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

POLYMARKET_BASE = "https://polymarket.com"


def market_url(obj: dict) -> Optional[str]:
    """Best-effort public Polymarket page URL for a Gamma market/event object.

    Polymarket pages live at ``/event/<slug>``. A market is part of an event, so
    prefer the parent event's slug (``events[0].slug``) and fall back to the
    object's own ``slug``. Returns None when no slug is present, so callers can
    simply omit the link rather than emit a broken one.
    """
    slug = None
    events = obj.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        slug = events[0].get("slug")
    slug = slug or obj.get("slug")
    return f"{POLYMARKET_BASE}/event/{slug}" if slug else None


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


def _levels(entries) -> list[Level]:
    """Parse a raw CLOB price-level list into clean ``Level``s (bad rows dropped)."""
    out: list[Level] = []
    for entry in entries or []:
        price = _to_float(entry.get("price"))
        size = _to_float(entry.get("size"))
        if price is None or size is None or size <= 0:
            continue
        out.append(Level(price=price, size=size))
    return out


def book_ladders(raw_book: Optional[dict]) -> tuple[list[Level], list[Level]]:
    """Full (asks, bids) ladders from a raw CLOB ``/book`` response.

    Asks ascending by price, bids descending — the order the realism layer walks
    them. Empty lists when the book is missing, so callers degrade to top-of-book.
    """
    if not raw_book:
        return [], []
    asks = sorted(_levels(raw_book.get("asks")), key=lambda lv: lv.price)
    bids = sorted(_levels(raw_book.get("bids")), key=lambda lv: lv.price, reverse=True)
    return asks, bids


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
        raw = books_by_token.get(token_id)
        best_ask, best_bid = top_of_book(raw)
        asks, bids = book_ladders(raw)
        legs.append(
            Leg(
                token_id=token_id, outcome=outcome,
                best_ask=best_ask, best_bid=best_bid, asks=asks, bids=bids,
            )
        )

    return CompleteSet(
        market_id=str(market.get("id", "")),
        question=str(market.get("question", "")),
        legs=legs,
        neg_risk=bool(market.get("negRisk", False)),
        exhaustive=True,  # a single binary market is exhaustive by construction
        end_date=market.get("endDate"),
        url=market_url(market),
    )


def indicative_set_cost(market: dict) -> Optional[float]:
    """Sum of indicative ``outcomePrices`` — a cheap pre-filter, not executable."""
    prices = [_to_float(p) for p in _maybe_json_list(market.get("outcomePrices"))]
    prices = [p for p in prices if p is not None]
    if not prices:
        return None
    return sum(prices)


# --------------------------------------------------------------------------- #
# Negative-risk event groups (multi-candidate markets)
# --------------------------------------------------------------------------- #
#
# A negative-risk event (e.g. "Who wins the election?") groups several binary
# sub-markets — one "Will candidate X win?" per candidate. The candidates are
# mutually exclusive and, for negRisk events, collectively exhaustive, so the
# "Yes" tokens across all sub-markets form a complete set: buy one Yes of each
# for < $1 total and exactly one settles to $1.
#
# Each sub-market has outcomes ["Yes", "No"] and clobTokenIds aligned to them;
# we take the "Yes" token from each. Labels prefer Gamma's groupItemTitle.


def _yes_index(outcomes: list) -> int:
    for i, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() in ("yes", "true"):
            return i
    return 0


def submarket_yes_token(market: dict) -> Optional[str]:
    """The token id of a sub-market's "Yes" outcome (for batch book fetching)."""
    token_ids = [str(t) for t in _maybe_json_list(market.get("clobTokenIds"))]
    if not token_ids:
        return None
    outcomes = [str(o) for o in _maybe_json_list(market.get("outcomes"))]
    if len(token_ids) == len(outcomes):
        return token_ids[_yes_index(outcomes)]
    return token_ids[0]


def _yes_leg_from_submarket(
    market: dict, books_by_token: dict[str, dict]
) -> Optional[Leg]:
    token_id = submarket_yes_token(market)
    if token_id is None:
        return None
    raw = books_by_token.get(token_id)
    best_ask, best_bid = top_of_book(raw)
    asks, bids = book_ladders(raw)
    label = (
        market.get("groupItemTitle")
        or market.get("question")
        or token_id
    )
    return Leg(
        token_id=token_id, outcome=str(label),
        best_ask=best_ask, best_bid=best_bid, asks=asks, bids=bids,
    )


def complete_set_from_event(
    event: dict, books_by_token: dict[str, dict]
) -> Optional[CompleteSet]:
    """Build a complete set from a multi-candidate event's "Yes" legs.

    Returns None if fewer than two parseable legs are found. ``exhaustive`` is
    only set True for negRisk events, where the candidate set is designed to be
    collectively exhaustive; for other events the buy-all-Yes payoff is not a
    guaranteed $1 and the caller should treat it with care.
    """
    legs: list[Leg] = []
    for submarket in event.get("markets") or []:
        leg = _yes_leg_from_submarket(submarket, books_by_token)
        if leg is not None:
            legs.append(leg)
    if len(legs) < 2:
        return None

    neg_risk = bool(event.get("negRisk", False))
    return CompleteSet(
        market_id=str(event.get("id", "")),
        question=str(event.get("title") or event.get("question") or ""),
        legs=legs,
        neg_risk=neg_risk,
        exhaustive=neg_risk,
        end_date=event.get("endDate"),
        url=market_url(event),
    )


def indicative_event_cost(event: dict) -> Optional[float]:
    """Sum of indicative "Yes" prices across an event's sub-markets (pre-filter)."""
    total = 0.0
    found = False
    for submarket in event.get("markets") or []:
        outcomes = [str(o) for o in _maybe_json_list(submarket.get("outcomes"))]
        prices = [_to_float(p) for p in _maybe_json_list(submarket.get("outcomePrices"))]
        if not prices:
            continue
        idx = _yes_index(outcomes) if len(outcomes) == len(prices) else 0
        if idx < len(prices) and prices[idx] is not None:
            total += prices[idx]
            found = True
    return total if found else None
