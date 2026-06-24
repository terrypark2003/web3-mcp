"""Orchestration: fetch -> normalize -> detect -> rank -> report.

The live path is intentionally efficient: it pulls the (cheap) market list
from Gamma, pre-filters on indicative prices, and only fetches live order
books for the handful of markets that look close to an arbitrage. Order
books are the source of truth for the actual decision.
"""

from __future__ import annotations

import json
from typing import Optional

from .detect import FeeModel, scan_sets
from .models import ARB_BUY_SET, CompleteSet, Opportunity
from .normalize import (
    _maybe_json_list,
    complete_set_from_event,
    complete_set_from_market,
    indicative_event_cost,
    indicative_set_cost,
    submarket_yes_token,
)


def build_sets_live(
    client,
    fees: FeeModel,
    limit: Optional[int] = None,
    prefilter_margin: float = 0.02,
) -> list[CompleteSet]:
    """Fetch markets + negRisk event groups, pre-filter, and fetch order books.

    Two kinds of complete set are assembled:

    * Binary markets — each market's [Yes, No] tokens (2-leg sets).
    * Negative-risk event groups — the "Yes" token of every candidate
      sub-market (N-leg sets). Only negRisk events are grouped, because only
      they are designed to be collectively exhaustive (a buy-all-Yes basket is
      a guaranteed $1 only when the candidates are exhaustive).

    The cheap pre-filter keeps anything whose indicative prices sum below
    1 + margin (or that has no indicative price), so we only spend order-book
    requests on plausible arbitrages.
    """
    # --- Binary markets ---------------------------------------------------- #
    market_candidates = []
    for market in client.active_markets():
        cost = indicative_set_cost(market)
        if cost is None or cost <= 1.0 + prefilter_margin:
            market_candidates.append(market)

    # --- Negative-risk event groups --------------------------------------- #
    try:
        events = client.active_events()
    except Exception:  # noqa: BLE001 - events are additive; a failure shouldn't abort
        events = []
    event_candidates = []
    for event in events:
        if not event.get("negRisk"):
            continue
        cost = indicative_event_cost(event)
        if cost is None or cost <= 1.0 + prefilter_margin:
            event_candidates.append(event)

    if limit is not None:
        market_candidates = market_candidates[:limit]
        event_candidates = event_candidates[:limit]

    # Collect every token id we need a book for, then batch-fetch once.
    token_ids: list[str] = []
    for market in market_candidates:
        token_ids.extend(str(t) for t in _maybe_json_list(market.get("clobTokenIds")))
    for event in event_candidates:
        for submarket in event.get("markets") or []:
            token = submarket_yes_token(submarket)
            if token:
                token_ids.append(token)
    books_by_token = client.order_books(token_ids)

    sets: list[CompleteSet] = []
    for market in market_candidates:
        cs = complete_set_from_market(market, books_by_token)
        if cs is not None:
            sets.append(cs)
    for event in event_candidates:
        cs = complete_set_from_event(event, books_by_token)
        if cs is not None:
            sets.append(cs)
    return sets


def scan_live(
    client,
    fees: Optional[FeeModel] = None,
    limit: Optional[int] = None,
) -> list[Opportunity]:
    fees = fees or FeeModel()
    sets = build_sets_live(client, fees, limit=limit)
    return scan_sets(sets, fees)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def opportunity_to_dict(op: Opportunity) -> dict:
    return {
        "kind": op.kind,
        "market_id": op.market_id,
        "question": op.question,
        "neg_risk": op.neg_risk,
        "exhaustive": op.exhaustive,
        "end_date": op.end_date,
        "n_legs": op.n_legs,
        "cost_per_set": round(op.cost_per_set, 4),
        "proceeds_per_set": round(op.proceeds_per_set, 4),
        "edge_per_set": round(op.edge_per_set, 4),
        "edge_pct": round(op.edge_pct, 2),
        "max_sets": round(op.max_sets, 2),
        "capital_required": round(op.capital_required, 2),
        "total_edge": round(op.total_edge, 2),
        "annualized_pct": None if op.annualized_pct is None else round(op.annualized_pct, 1),
        "url": op.url,
        "legs": [
            {
                "outcome": leg.outcome,
                "token_id": leg.token_id,
                "ask": None if leg.best_ask is None else round(leg.best_ask.price, 4),
                "bid": None if leg.best_bid is None else round(leg.best_bid.price, 4),
            }
            for leg in op.legs
        ],
    }


def format_table(opportunities: list[Opportunity]) -> str:
    if not opportunities:
        return "No arbitrage opportunities found above the configured thresholds."

    header = (
        f"{'KIND':<10} {'EDGE/SET':>9} {'EDGE%':>7} {'MAX$':>9} "
        f"{'TOT$':>8} {'APR%':>8}  QUESTION"
    )
    lines = [header, "-" * len(header)]
    for op in opportunities:
        apr = "instant" if op.annualized_pct is None else f"{op.annualized_pct:,.0f}"
        question = op.question if len(op.question) <= 48 else op.question[:45] + "..."
        flag = "" if op.exhaustive else "  [!exhaustive-unconfirmed]"
        lines.append(
            f"{op.kind:<10} {op.edge_per_set:>9.4f} {op.edge_pct:>6.2f}% "
            f"{op.capital_required:>9.2f} {op.total_edge:>8.2f} {apr:>8}  "
            f"{question}{flag}"
        )
    note = (
        "\nBUY_SET locks capital until resolution (APR shown). MINT_SELL is "
        "instant. Edges are gross of gas/slippage; confirm depth before sizing."
    )
    return "\n".join(lines) + "\n" + note


def write_json(opportunities: list[Opportunity], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([opportunity_to_dict(op) for op in opportunities], fh, indent=2)


def cross_to_dict(op) -> dict:
    """Serialize a crossvenue.CrossVenueOpportunity for JSON / the web API."""
    return {
        "kind": op.kind,
        "event_id": op.event_id,
        "question": op.question,
        "end_date": op.end_date,
        "yes_venue": op.yes_venue,
        "no_venue": op.no_venue,
        "yes_price": round(op.yes_price, 4),
        "no_price": round(op.no_price, 4),
        "cost_per_set": round(op.cost_per_set, 4),
        "fee_per_set": round(op.fee_per_set, 4),
        "edge_per_set": round(op.edge_per_set, 4),
        "edge_pct": round(op.edge_pct, 2),
        "max_sets": round(op.max_sets, 2),
        "capital_required": round(op.capital_required, 2),
        "total_edge": round(op.total_edge, 2),
        "annualized_pct": None if op.annualized_pct is None else round(op.annualized_pct, 1),
    }


def ev_to_dict(op) -> dict:
    """Serialize an ev.EVOpportunity for JSON / the web API."""
    return {
        "kind": op.kind,
        "market_id": op.market_id,
        "question": op.question,
        "venue": op.venue,
        "side": op.side,
        "price": round(op.price, 4),
        "fair_prob": round(op.fair_prob, 4),
        "ev_per_contract": round(op.ev_per_contract, 4),
        "edge_pct": round(op.edge_pct, 2),
        "max_size": round(op.max_size, 2),
        "end_date": op.end_date,
        "url": op.url,
    }


def format_cross_table(opportunities) -> str:
    """Render cross-venue opportunities (see crossvenue.CrossVenueOpportunity)."""
    if not opportunities:
        return "No cross-venue arbitrage found above thresholds."
    header = (
        f"{'EDGE/SET':>9} {'EDGE%':>7} {'FEE/SET':>8} {'MAX$':>9} {'TOT$':>8}  "
        f"BUY YES / BUY NO   QUESTION"
    )
    lines = [header, "-" * len(header)]
    for op in opportunities:
        q = op.question if len(op.question) <= 36 else op.question[:33] + "..."
        lines.append(
            f"{op.edge_per_set:>9.4f} {op.edge_pct:>6.2f}% {op.fee_per_set:>8.4f} "
            f"{op.capital_required:>9.2f} {op.total_edge:>8.2f}  "
            f"YES@{op.yes_venue[:4]} {op.yes_price:.2f}/NO@{op.no_venue[:4]} "
            f"{op.no_price:.2f}  {q}"
        )
    lines.append(
        "\nRisk-free ONLY if both venues resolve identically (same source/cutoff). "
        "Fees are modeled; confirm depth + rulebooks before sizing."
    )
    return "\n".join(lines)


def format_ev_table(opportunities) -> str:
    """Render positive-EV signals (see ev.EVOpportunity). NOT risk-free."""
    if not opportunities:
        return "No positive-EV signals above threshold."
    header = f"{'EV/CT':>7} {'EDGE%':>7} {'PRICE':>6} {'FAIR':>6} {'MAX':>7}  SIDE VENUE   QUESTION"
    lines = [header, "-" * len(header)]
    for op in opportunities:
        q = op.question if len(op.question) <= 34 else op.question[:31] + "..."
        lines.append(
            f"{op.ev_per_contract:>7.3f} {op.edge_pct:>6.1f}% {op.price:>6.2f} "
            f"{op.fair_prob:>6.2f} {op.max_size:>7.0f}  {op.side:<4} "
            f"{op.venue[:6]:<6}  {q}"
        )
    lines.append(
        "\nNOT risk-free: EV is only as good as the fair-value estimate, and any "
        "single bet can lose in full. Sizing should reflect that (e.g. Kelly)."
    )
    return "\n".join(lines)
