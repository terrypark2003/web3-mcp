"""Positive expected-value (EV) finder.

Arbitrage is risk-free; this is NOT. The pitch — "prices can be wrong, and if
you spot it you have an edge" — only holds if your estimate of the true
probability is better than the market's. So EV here is an *opinion*, only as
good as the fair-value number you feed in, and any single bet can lose in full.

Given a binary market and a fair probability ``p`` for YES:

    Buy YES at ask a_yes:  EV/contract = p*1 - a_yes - fee
    Buy NO  at ask a_no:   EV/contract = (1-p)*1 - a_no - fee

We surface the side whose EV clears a threshold, expressed both per contract
and as an edge percent over the price paid. The ``FairValue`` source is
deliberately pluggable: today it's a curated map (you supply the numbers); a
model or data feed can replace it without touching the detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import CompleteSet, Leg
from .normalize import _yes_index
from .venues import VenueFee, fee_for

EV_SIGNAL = "POSITIVE_EV"

# A fair-value source maps a market id -> estimated P(YES) in [0, 1], or None.
FairValue = Callable[[str], Optional[float]]


@dataclass
class EVOpportunity:
    """A positive-EV bet on one side of one market (an opinion, not risk-free)."""

    kind: str
    market_id: str
    question: str
    venue: str
    side: str               # "YES" or "NO"
    price: float            # ask paid per contract
    fair_prob: float        # estimated P(side wins)
    ev_per_contract: float  # fair_prob - price - fee
    edge_pct: float         # ev relative to price paid, percent
    max_size: float         # depth available at that ask
    end_date: Optional[str]
    url: Optional[str] = None  # public market page (to open/trade), if known


def fair_value_from_map(probs: dict[str, float]) -> FairValue:
    """Build a FairValue source from a market_id -> P(YES) mapping."""
    def source(market_id: str) -> Optional[float]:
        return probs.get(market_id)
    return source


def _ev_for_side(
    leg: Leg,
    venue: str,
    side: str,
    win_prob: float,
    fee_model: VenueFee,
    min_ev: float,
    min_size: float,
    market_id: str,
    question: str,
    end_date: Optional[str],
    url: Optional[str] = None,
) -> Optional[EVOpportunity]:
    if leg.best_ask is None:
        return None
    price = leg.best_ask.price
    size = leg.best_ask.size
    if size < min_size:
        return None
    fee = fee_model.fee(price, 1.0)
    ev = win_prob - price - fee
    if ev < min_ev:
        return None
    return EVOpportunity(
        kind=EV_SIGNAL,
        market_id=market_id,
        question=question,
        venue=venue,
        side=side,
        price=price,
        fair_prob=win_prob,
        ev_per_contract=ev,
        edge_pct=(ev / price * 100) if price > 0 else 0.0,
        max_size=size,
        end_date=end_date,
        url=url,
    )


def detect_ev(
    cs: CompleteSet,
    fair: FairValue,
    fees: Optional[dict[str, VenueFee]] = None,
    min_ev: float = 0.02,
    min_size: float = 1.0,
) -> list[EVOpportunity]:
    """Find positive-EV sides of one binary market given a fair-value source."""
    if len(cs.legs) != 2:
        return []
    p_yes = fair(cs.market_id)
    if p_yes is None or not (0.0 <= p_yes <= 1.0):
        return []

    fee_model = fee_for(cs.venue, fees or {})
    yi = _yes_index([leg.outcome for leg in cs.legs])
    yes_leg, no_leg = cs.legs[yi], cs.legs[1 - yi]

    out: list[EVOpportunity] = []
    for leg, side, win_prob in (
        (yes_leg, "YES", p_yes),
        (no_leg, "NO", 1.0 - p_yes),
    ):
        op = _ev_for_side(
            leg, cs.venue, side, win_prob, fee_model, min_ev, min_size,
            cs.market_id, cs.question, cs.end_date, cs.url,
        )
        if op is not None:
            out.append(op)
    return out


def scan_ev(
    sets,
    fair: FairValue,
    fees: Optional[dict[str, VenueFee]] = None,
    min_ev: float = 0.02,
    min_size: float = 1.0,
) -> list[EVOpportunity]:
    """Detect positive-EV bets across many markets, ranked by EV (descending)."""
    out: list[EVOpportunity] = []
    for cs in sets:
        out.extend(detect_ev(cs, fair, fees, min_ev, min_size))
    out.sort(key=lambda o: o.ev_per_contract, reverse=True)
    return out
