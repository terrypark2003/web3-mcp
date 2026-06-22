"""Cross-venue arbitrage: the same event priced differently on two exchanges.

This is the edge Asymmetric-style traders chase between regulated and crypto
prediction markets. For one binary event traded on both Kalshi and Polymarket:

    Buy YES on the cheaper venue and NO on the other. Exactly one settles to
    $1, so if (yes_ask on venue X) + (no_ask on venue Y) < $1 after fees, the
    pair is a locked profit regardless of outcome.

We evaluate both pairings (YES@A+NO@B and YES@B+NO@A) and keep the better one.
A YES at price ``p`` and a NO at price ``q`` on the *other* venue cost ``p+q``
up front and return $1 at resolution.

THE CRITICAL CAVEAT — resolution risk
-------------------------------------
This is only risk-free if both venues resolve the event *identically*: same
question, same resolution source, same settlement timing. Two markets that look
like "will X happen?" can resolve differently (different cutoff, different
source of truth). The matching layer (``matching.py``) pairs markets by an
explicit, human-curated registry for exactly this reason — never by fuzzy text
match alone. Treat every cross-venue pair as resolution-risked until you have
read both rulebooks.

Fees are modeled per venue (``venues.py``); Kalshi's per-contract fee is large
enough near 50¢ to erase most raw gaps, so it is applied before reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .detect import _years_until
from .models import CompleteSet, Leg
from .normalize import _yes_index
from .venues import VenueFee, default_venue_fees, fee_for

ARB_CROSS_VENUE = "CROSS_VENUE"


@dataclass
class CrossVenueOpportunity:
    """A cross-venue arbitrage with both legs and net economics computed."""

    kind: str
    event_id: str
    question: str
    end_date: Optional[str]
    yes_venue: str               # venue we buy YES on
    no_venue: str                # venue we buy NO on
    yes_price: float
    no_price: float
    cost_per_set: float          # yes_price + no_price (gross, before fees)
    fee_per_set: float           # blended fee per set at the chosen size
    edge_per_set: float          # net USD profit per set after fees
    edge_pct: float              # edge relative to capital deployed, percent
    max_sets: float              # depth-limited (thinner of the two legs)
    capital_required: float
    total_edge: float
    annualized_pct: Optional[float]
    legs: list[Leg] = field(default_factory=list)


@dataclass
class MatchedMarket:
    """One event paired across two venues, each a binary [Yes, No] CompleteSet."""

    event_id: str
    question: str
    venue_a: CompleteSet
    venue_b: CompleteSet
    end_date: Optional[str] = None


def _yes_no_legs(cs: CompleteSet) -> Optional[tuple[Leg, Leg]]:
    """Return (yes_leg, no_leg) for a binary set, or None if not parseable."""
    if len(cs.legs) != 2:
        return None
    yi = _yes_index([leg.outcome for leg in cs.legs])
    yes_leg = cs.legs[yi]
    no_leg = cs.legs[1 - yi]
    return yes_leg, no_leg


def _pairing(
    yes_leg: Leg,
    no_leg: Leg,
    yes_venue: str,
    no_venue: str,
    fees: dict[str, VenueFee],
) -> Optional[dict]:
    """Economics of buying ``yes_leg`` and ``no_leg`` from opposite venues."""
    if yes_leg.best_ask is None or no_leg.best_ask is None:
        return None
    yes_price = yes_leg.best_ask.price
    no_price = no_leg.best_ask.price
    max_sets = min(yes_leg.best_ask.size, no_leg.best_ask.size)
    if max_sets <= 0:
        return None

    gross_cost = (yes_price + no_price) * max_sets
    fee = (
        fee_for(yes_venue, fees).fee(yes_price, max_sets)
        + fee_for(no_venue, fees).fee(no_price, max_sets)
    )
    proceeds = 1.0 * max_sets
    total_edge = proceeds - gross_cost - fee
    return {
        "yes_leg": yes_leg,
        "no_leg": no_leg,
        "yes_venue": yes_venue,
        "no_venue": no_venue,
        "yes_price": yes_price,
        "no_price": no_price,
        "max_sets": max_sets,
        "gross_cost": gross_cost,
        "fee": fee,
        "total_edge": total_edge,
    }


def detect_cross_venue(
    matched: MatchedMarket,
    fees: Optional[dict[str, VenueFee]] = None,
    min_edge_per_set: float = 0.005,
    min_size: float = 1.0,
    now: Optional[datetime] = None,
) -> Optional[CrossVenueOpportunity]:
    """Best cross-venue pair for one matched event, or None if no edge clears."""
    fees = fees or default_venue_fees()
    a = _yes_no_legs(matched.venue_a)
    b = _yes_no_legs(matched.venue_b)
    if a is None or b is None:
        return None
    yes_a, no_a = a
    yes_b, no_b = b
    va, vb = matched.venue_a.venue, matched.venue_b.venue

    candidates = [
        _pairing(yes_a, no_b, va, vb, fees),  # YES on A, NO on B
        _pairing(yes_b, no_a, vb, va, fees),  # YES on B, NO on A
    ]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None

    best = max(candidates, key=lambda c: c["total_edge"])
    sets = best["max_sets"]
    edge_per_set = best["total_edge"] / sets if sets else 0.0
    if edge_per_set < min_edge_per_set or sets < min_size:
        return None

    cost_per_set = best["yes_price"] + best["no_price"]
    edge_fraction = best["total_edge"] / best["gross_cost"] if best["gross_cost"] else 0.0
    years = _years_until(matched.end_date, now)
    annualized = (edge_fraction / years) * 100 if years else None

    return CrossVenueOpportunity(
        kind=ARB_CROSS_VENUE,
        event_id=matched.event_id,
        question=matched.question,
        end_date=matched.end_date,
        yes_venue=best["yes_venue"],
        no_venue=best["no_venue"],
        yes_price=best["yes_price"],
        no_price=best["no_price"],
        cost_per_set=cost_per_set,
        fee_per_set=best["fee"] / sets if sets else 0.0,
        edge_per_set=edge_per_set,
        edge_pct=edge_fraction * 100,
        max_sets=sets,
        capital_required=best["gross_cost"],
        total_edge=best["total_edge"],
        annualized_pct=annualized,
        legs=[best["yes_leg"], best["no_leg"]],
    )


def scan_cross_venue(
    matched_markets,
    fees: Optional[dict[str, VenueFee]] = None,
    min_edge_per_set: float = 0.005,
    min_size: float = 1.0,
    now: Optional[datetime] = None,
) -> list[CrossVenueOpportunity]:
    """Detect across many matched events, ranked by total edge (descending)."""
    out: list[CrossVenueOpportunity] = []
    for matched in matched_markets:
        op = detect_cross_venue(matched, fees, min_edge_per_set, min_size, now)
        if op is not None:
            out.append(op)
    out.sort(key=lambda o: o.total_edge, reverse=True)
    return out
