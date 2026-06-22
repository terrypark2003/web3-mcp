"""Translate raw Kalshi API JSON into the normalized ``CompleteSet`` model.

Like ``normalize.py`` is for Polymarket, this is the ONLY module that encodes
assumptions about Kalshi's response shapes. A Kalshi binary market maps onto a
2-leg [Yes, No] CompleteSet (venue="kalshi"), so all the existing detectors
work on it unchanged.

Assumptions (verify against a live snapshot — see ``KalshiClient``):

* ``GET /markets/{ticker}`` -> ``{"market": {...}}`` with:
    - ``ticker``       unique market id, e.g. "KXPRES-24-DJT"
    - ``title`` / ``subtitle``  human-readable question
    - ``close_time``   ISO-8601 resolution/close time
    - ``status``       "active" when tradeable
    - indicative ``yes_ask`` / ``no_ask`` in CENTS (1-99)

* ``GET /markets/{ticker}/orderbook`` -> ``{"orderbook": {"yes": [...], "no": [...]}}``
  where each side is a list of ``[price_cents, size]`` RESTING BIDS:
    - ``yes`` = bids to buy YES, ``no`` = bids to buy NO.

The key translation: Kalshi quotes resting *bids* only, and YES/NO are two
sides of the same contract (they sum to $1). So to BUY YES as a taker you lift
the best NO bid:

    yes_ask = $1 - best_no_bid     no_ask = $1 - best_yes_bid
    yes_bid = best_yes_bid         no_bid = best_no_bid

Prices are converted cents -> dollars (0-1) to match the Polymarket side.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import CompleteSet, Leg, Level
from .venues import KALSHI


def _best_bid_cents(levels: Any) -> Optional[tuple[float, float]]:
    """Return (best_price_cents, size) from a Kalshi [[price, size], ...] side."""
    best_price = None
    best_size = 0.0
    for entry in levels or []:
        try:
            price = float(entry[0])
            size = float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if size <= 0:
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_size = size
    if best_price is None:
        return None
    return best_price, best_size


def complete_set_from_kalshi(
    market: dict, orderbook: Optional[dict]
) -> Optional[CompleteSet]:
    """Build a 2-leg [Yes, No] CompleteSet from a Kalshi market + its order book.

    Returns None if the ticker is missing. Missing book sides yield legs with
    ``None`` quotes, which the detectors treat as "no executable price".
    """
    ticker = market.get("ticker")
    if not ticker:
        return None
    ticker = str(ticker)

    book = (orderbook or {}).get("orderbook", orderbook or {})
    yes_bid = _best_bid_cents(book.get("yes"))
    no_bid = _best_bid_cents(book.get("no"))

    yes_ask = no_ask = None
    yes_bid_lvl = no_bid_lvl = None
    if yes_bid is not None:
        price_c, size = yes_bid
        yes_bid_lvl = Level(price=price_c / 100.0, size=size)
        no_ask = Level(price=(100.0 - price_c) / 100.0, size=size)
    if no_bid is not None:
        price_c, size = no_bid
        no_bid_lvl = Level(price=price_c / 100.0, size=size)
        yes_ask = Level(price=(100.0 - price_c) / 100.0, size=size)

    title = market.get("title") or market.get("subtitle") or ticker
    legs = [
        Leg(f"{ticker}:YES", "Yes", best_ask=yes_ask, best_bid=yes_bid_lvl, venue=KALSHI),
        Leg(f"{ticker}:NO", "No", best_ask=no_ask, best_bid=no_bid_lvl, venue=KALSHI),
    ]
    return CompleteSet(
        market_id=ticker,
        question=str(title),
        legs=legs,
        neg_risk=False,
        exhaustive=True,  # a single binary contract is exhaustive by construction
        end_date=market.get("close_time"),
        venue=KALSHI,
    )


def indicative_kalshi_cost(market: dict) -> Optional[float]:
    """Sum of indicative yes_ask + no_ask (cents -> dollars), a cheap pre-filter."""
    yes_ask = market.get("yes_ask")
    no_ask = market.get("no_ask")
    if yes_ask is None or no_ask is None:
        return None
    try:
        return (float(yes_ask) + float(no_ask)) / 100.0
    except (TypeError, ValueError):
        return None
