"""Cross-venue event matching — an explicit, curated registry.

Pairing "the same event" across Kalshi and Polymarket is the hard part of
cross-venue arbitrage, and getting it wrong is how a "risk-free" trade turns
into two uncorrelated bets. Two markets can share a headline yet resolve on
different sources, cutoffs, or definitions. So this module does NOT guess: it
reads a human-curated registry of pairs, each asserting "these two market ids
resolve identically." Fuzzy auto-matching (by title similarity) is a possible
future aid, but a human must confirm a pair before it's traded.

Registry entry (see ``fixtures/cross_venue_pairs.json``):

    {
      "event_id": "btc-100k-2026",
      "question": "Will BTC close above $100k in 2026?",
      "kalshi_ticker": "KXBTC-26-100K",
      "polymarket_market_id": "0xabc...",
      "end_date": "2026-12-31T23:59:59Z"
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from .crossvenue import MatchedMarket
from .models import CompleteSet

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "cross_venue_pairs.json"
)


@dataclass
class Pair:
    """One curated cross-venue pairing of market ids asserted to co-resolve."""

    event_id: str
    question: str
    kalshi_ticker: str
    polymarket_market_id: str
    end_date: Optional[str] = None


def load_pairs(path: str = _FIXTURE) -> list[Pair]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [
        Pair(
            event_id=entry["event_id"],
            question=entry["question"],
            kalshi_ticker=entry["kalshi_ticker"],
            polymarket_market_id=entry["polymarket_market_id"],
            end_date=entry.get("end_date"),
        )
        for entry in data.get("pairs", [])
    ]


def build_matched_markets(
    pairs: list[Pair],
    kalshi_by_ticker: dict[str, CompleteSet],
    poly_by_id: dict[str, CompleteSet],
) -> list[MatchedMarket]:
    """Join curated pairs against the live/normalized sets from each venue.

    Pairs whose markets are missing on either venue are skipped (you can't
    arb a leg you can't price).
    """
    matched: list[MatchedMarket] = []
    for pair in pairs:
        kalshi = kalshi_by_ticker.get(pair.kalshi_ticker)
        poly = poly_by_id.get(pair.polymarket_market_id)
        if kalshi is None or poly is None:
            continue
        matched.append(
            MatchedMarket(
                event_id=pair.event_id,
                question=pair.question,
                venue_a=kalshi,
                venue_b=poly,
                end_date=pair.end_date or kalshi.end_date or poly.end_date,
            )
        )
    return matched
