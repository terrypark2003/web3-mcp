"""Near-resolution "favorites" finder — markets settling soon where one outcome
is a strong favorite you can buy for roughly a 1.1x payout.

WHAT THIS IS (AND IS NOT)
-------------------------
This answers the literal ask: "show me bets that resolve in a day or two where
$1 pays about $1.1." Buying an outcome at price ``p`` returns $1 per share if it
wins, so the payout multiple is ``1/p`` — ``p ≈ 0.91`` is the "$1 → $1.10" band.

This is **NOT arbitrage and NOT a value edge.** The price IS the market's implied
probability, so an 88¢ favorite is just ~88% likely; buying it earns ~13% if it
lands and loses the whole stake if it doesn't. There is no claimed edge here —
only a short time to resolution and a high (market-implied) win probability.
Risk-free $1→$1.1 is the job of the arbitrage scanner (which almost never finds
a full 10%); this is the opposite trade-off: likely, soon, small, and risked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class FavoriteBet:
    """A soon-resolving outcome priced like a favorite (NOT risk-free)."""

    market_id: str
    question: str
    outcome: str
    price: float              # best ask (USDC per share) = market-implied prob
    payout_multiple: float    # 1 / price  ("$1 -> $payout_multiple" if it wins)
    implied_prob: float       # == price; what the market thinks is the win chance
    max_size: float           # shares available at that ask
    end_date: Optional[str]
    days_to_resolution: Optional[float]
    url: Optional[str] = None


def days_until(end_date, now: Optional[datetime] = None) -> Optional[float]:
    """Days from ``now`` to ``end_date`` (negative if past); None if unparseable."""
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 86400


def find_favorites(
    sets,
    *,
    min_price: float = 0.80,
    max_price: float = 0.91,
    min_size: float = 1.0,
    max_days: Optional[float] = 2.0,
    now: Optional[datetime] = None,
) -> list[FavoriteBet]:
    """Outcomes priced in ``[min_price, max_price]`` that resolve within ``max_days``.

    ``max_price`` is the payout ceiling: 0.91 ≈ a 1.1x payout. ``min_price`` keeps
    the list to genuine favorites (a 0.50 coin-flip is not "$1 → $1.1"). One leg
    per market typically qualifies (the favorite side). Sorted soonest-first,
    then by payout.
    """
    out: list[FavoriteBet] = []
    for cs in sets:
        days = days_until(cs.end_date, now)
        if max_days is not None and (days is None or not (0 <= days <= max_days)):
            continue
        for leg in cs.legs:
            if leg.best_ask is None:
                continue
            p = leg.best_ask.price
            if not (min_price <= p <= max_price):
                continue
            if leg.best_ask.size < min_size:
                continue
            out.append(FavoriteBet(
                market_id=cs.market_id,
                question=cs.question,
                outcome=leg.outcome,
                price=p,
                payout_multiple=(1.0 / p) if p > 0 else 0.0,
                implied_prob=p,
                max_size=leg.best_ask.size,
                end_date=cs.end_date,
                days_to_resolution=days,
                url=cs.url,
            ))
    out.sort(key=lambda f: (
        f.days_to_resolution if f.days_to_resolution is not None else 1e9,
        -f.payout_multiple,
    ))
    return out


def favorite_to_dict(f: FavoriteBet) -> dict:
    """Serialize for JSON / the notifier."""
    return {
        "market_id": f.market_id,
        "question": f.question,
        "outcome": f.outcome,
        "price": round(f.price, 4),
        "payout_multiple": round(f.payout_multiple, 3),
        "implied_prob": round(f.implied_prob, 4),
        "max_size": round(f.max_size, 2),
        "end_date": f.end_date,
        "days_to_resolution": (
            None if f.days_to_resolution is None else round(f.days_to_resolution, 2)
        ),
        "url": f.url,
    }


def build_favorites_live(
    client,
    *,
    min_price: float = 0.80,
    max_price: float = 0.91,
    min_size: float = 1.0,
    max_days: Optional[float] = 2.0,
    now: Optional[datetime] = None,
) -> list[FavoriteBet]:
    """Fetch active markets resolving within ``max_days``, confirm favorites on the book.

    Cheap pre-filter on Gamma's indicative prices + end date keeps order-book
    fetches bounded; the live book is the source of truth for the final decision.
    """
    from .normalize import (
        _maybe_json_list,
        _to_float,
        complete_set_from_market,
    )

    # Loosen the pre-filter band slightly so a market that's marginally outside
    # on indicative price still gets a book fetch (the book is authoritative).
    lo, hi = min_price - 0.04, max_price + 0.04

    candidates = []
    for market in client.active_markets():
        days = days_until(market.get("endDate"), now)
        if days is None or not (0 <= days <= (max_days if max_days is not None else 1e9)):
            continue
        prices = [_to_float(p) for p in _maybe_json_list(market.get("outcomePrices"))]
        if any(p is not None and lo <= p <= hi for p in prices):
            candidates.append(market)

    token_ids: list[str] = []
    for market in candidates:
        token_ids.extend(str(t) for t in _maybe_json_list(market.get("clobTokenIds")))
    books = client.order_books(token_ids)

    sets = [
        cs for cs in (complete_set_from_market(m, books) for m in candidates)
        if cs is not None
    ]
    return find_favorites(
        sets, min_price=min_price, max_price=max_price,
        min_size=min_size, max_days=max_days, now=now,
    )
