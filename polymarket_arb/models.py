"""Normalized data models the detection logic operates on.

These are deliberately decoupled from the Polymarket API response shapes.
The ``normalize`` module is the only place that knows about raw Gamma/CLOB
JSON; everything downstream works on these dataclasses, which keeps the
detection math pure and unit-testable without a network connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Opportunity kinds.
ARB_BUY_SET = "BUY_SET"        # buy 1 share of every leg now; redeem $1 at resolution
ARB_MINT_SELL = "MINT_SELL"    # mint a complete set for $1 now; sell every leg immediately


@dataclass
class Level:
    """Top-of-book price level for one outcome token."""

    price: float  # USDC per share, in [0, 1]
    size: float   # shares available at this price


@dataclass
class Leg:
    """One outcome token of a complete set, with its top-of-book quotes."""

    token_id: str
    outcome: str                 # "Yes" / "No" or a candidate label
    best_ask: Optional[Level]    # lowest ask  -> price to BUY one share
    best_bid: Optional[Level]    # highest bid -> proceeds to SELL one share


@dataclass
class CompleteSet:
    """A set of mutually-exclusive, collectively-exhaustive outcome tokens.

    Together the legs pay exactly $1 at resolution: exactly one leg settles
    to $1 and the rest to $0. A binary market is the 2-leg case ([Yes, No]);
    a Polymarket negative-risk event group is the N-leg case (one Yes token
    per candidate).
    """

    market_id: str
    question: str
    legs: list[Leg]
    neg_risk: bool = False
    exhaustive: bool = True      # are the legs *known* to be collectively exhaustive?
    end_date: Optional[str] = None  # ISO-8601 resolution time, if known


@dataclass
class Opportunity:
    """A detected arbitrage, with sizing and economics already computed."""

    kind: str
    market_id: str
    question: str
    neg_risk: bool
    exhaustive: bool
    end_date: Optional[str]
    n_legs: int
    cost_per_set: float          # USDC paid per set
    proceeds_per_set: float      # USDC received per set
    edge_per_set: float          # net USDC profit per set, after fees
    edge_pct: float              # edge relative to capital deployed, in percent
    max_sets: float              # depth-limited size (thinnest leg at top of book)
    capital_required: float      # USDC to realize ``max_sets``
    total_edge: float            # edge_per_set * max_sets
    annualized_pct: Optional[float]  # simple APR for held positions; None if instant
    legs: list[Leg] = field(default_factory=list)
