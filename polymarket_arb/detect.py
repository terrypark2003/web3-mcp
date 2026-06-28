"""Arbitrage detection — pure functions over normalized models.

The economics
-------------
A complete set of N mutually-exclusive, collectively-exhaustive outcomes
pays exactly $1 at resolution (one leg -> $1, the rest -> $0).

BUY_SET (hold to resolution):
    Buy one share of every leg at its best ask.
        cost   = sum(ask_i)
        payoff = $1 at resolution
        edge   = 1 - cost - fees
    Capital is locked until ``end_date``, so we also report a simple APR.

MINT_SELL (instant, binary only):
    Mint a complete set via the CTF split for $1, then immediately sell
    every leg at its best bid.
        proceeds = sum(bid_i)
        edge     = proceeds - 1 - fees
    This pays back instantly (no lockup), so APR is not meaningful.
    Restricted to standard binary sets, where the $1 split/merge is the
    well-understood mechanism. (Negative-risk mint/convert is left as an
    extension — see README.)

Sizing is depth-limited to the thinnest leg's top-of-book size, which is a
conservative lower bound (a full-book walk would usually allow more).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .models import (
    ARB_BUY_SET,
    ARB_MINT_SELL,
    CompleteSet,
    Opportunity,
)
from .realism import (
    confidence_score,
    executable_buy_set,
    executable_mint_sell,
)

_SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass
class FeeModel:
    """Costs and thresholds applied during detection.

    Polymarket's CLOB has historically charged no taker/maker fee on most
    markets, but that is not guaranteed per-market, and there is always
    Polygon gas plus execution slippage. Defaults are intentionally
    conservative so the scanner does not surface sub-cent noise as "free
    money".
    """

    taker_fee_rate: float = 0.0     # fraction of traded notional
    min_edge_per_set: float = 0.005  # USDC; ignore thinner edges (noise / model error)
    min_size: float = 1.0            # shares; ignore opportunities thinner than this
    gas_cost_usd: float = 0.0        # fixed on-chain cost per opportunity (redeem/split)
    min_order_usd: float = 1.0       # Polymarket's per-order $ floor (binds realism)


def _years_until(end_date: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Years from ``now`` until ``end_date`` (ISO-8601), or None if unknown/past."""
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    seconds = (dt - now).total_seconds()
    if seconds <= 0:
        return None
    return seconds / _SECONDS_PER_YEAR


def _score(ex, *, instant: bool, years, n_legs: int):
    """Confidence score for an executable, tolerating a missing book-walk."""
    if ex is None:
        return 0.0, ["호가 깊이 정보 없음 — 실행 가능성 미확인"]
    return confidence_score(ex, instant=instant, years=years, n_legs=n_legs)


def detect_buy_set(
    cs: CompleteSet, fees: FeeModel, now: Optional[datetime] = None
) -> Optional[Opportunity]:
    """Buy one share of every leg for < $1 total -> guaranteed $1 at resolution."""
    if not cs.legs or any(leg.best_ask is None for leg in cs.legs):
        return None

    cost = sum(leg.best_ask.price for leg in cs.legs)
    edge = 1.0 - cost - fees.taker_fee_rate * cost
    if edge < fees.min_edge_per_set:
        return None

    max_sets = min(leg.best_ask.size for leg in cs.legs)
    if max_sets < fees.min_size:
        return None

    edge_fraction = edge / cost if cost > 0 else 0.0
    years = _years_until(cs.end_date, now)
    annualized = (edge_fraction / years) * 100 if years else None

    ex = executable_buy_set(
        cs.legs,
        taker_fee_rate=fees.taker_fee_rate,
        gas_cost_usd=fees.gas_cost_usd,
        min_edge_per_set=fees.min_edge_per_set,
        min_order_usd=fees.min_order_usd,
    )
    confidence, reasons = _score(ex, instant=False, years=years, n_legs=len(cs.legs))

    return Opportunity(
        kind=ARB_BUY_SET,
        market_id=cs.market_id,
        question=cs.question,
        neg_risk=cs.neg_risk,
        exhaustive=cs.exhaustive,
        end_date=cs.end_date,
        n_legs=len(cs.legs),
        cost_per_set=cost,
        proceeds_per_set=1.0,
        edge_per_set=edge,
        edge_pct=edge_fraction * 100,
        max_sets=max_sets,
        capital_required=cost * max_sets,
        total_edge=edge * max_sets,
        annualized_pct=annualized,
        legs=list(cs.legs),
        url=cs.url,
        executable_sets=ex.executable_sets if ex else 0.0,
        executable_edge_per_set=ex.edge_per_set if ex else 0.0,
        net_total_edge=ex.net_total_edge if ex else 0.0,
        min_order_shares=ex.min_order_shares if ex else 0.0,
        feasible_min_order=ex.feasible_min_order if ex else False,
        confidence=confidence,
        confidence_reasons=reasons,
    )


def detect_mint_sell(
    cs: CompleteSet, fees: FeeModel, now: Optional[datetime] = None
) -> Optional[Opportunity]:
    """Mint a binary set for $1, sell both legs immediately for > $1."""
    # Restrict to standard binary sets where the $1 split/merge is well defined.
    if cs.neg_risk or len(cs.legs) != 2:
        return None
    if any(leg.best_bid is None for leg in cs.legs):
        return None

    proceeds = sum(leg.best_bid.price for leg in cs.legs)
    edge = proceeds - 1.0 - fees.taker_fee_rate * proceeds
    if edge < fees.min_edge_per_set:
        return None

    max_sets = min(leg.best_bid.size for leg in cs.legs)
    if max_sets < fees.min_size:
        return None

    ex = executable_mint_sell(
        cs.legs,
        taker_fee_rate=fees.taker_fee_rate,
        gas_cost_usd=fees.gas_cost_usd,
        min_edge_per_set=fees.min_edge_per_set,
        min_order_usd=fees.min_order_usd,
    )
    # MINT_SELL pays back instantly -> no lockup; that's its big realism edge.
    confidence, reasons = _score(ex, instant=True, years=None, n_legs=len(cs.legs))

    # $1 of capital per set, returned instantly -> APR is not meaningful.
    return Opportunity(
        kind=ARB_MINT_SELL,
        market_id=cs.market_id,
        question=cs.question,
        neg_risk=cs.neg_risk,
        exhaustive=cs.exhaustive,
        end_date=cs.end_date,
        n_legs=len(cs.legs),
        cost_per_set=1.0,
        proceeds_per_set=proceeds,
        edge_per_set=edge,
        edge_pct=edge * 100,  # relative to $1 deployed
        max_sets=max_sets,
        capital_required=1.0 * max_sets,
        total_edge=edge * max_sets,
        annualized_pct=None,
        legs=list(cs.legs),
        url=cs.url,
        executable_sets=ex.executable_sets if ex else 0.0,
        executable_edge_per_set=ex.edge_per_set if ex else 0.0,
        net_total_edge=ex.net_total_edge if ex else 0.0,
        min_order_shares=ex.min_order_shares if ex else 0.0,
        feasible_min_order=ex.feasible_min_order if ex else False,
        confidence=confidence,
        confidence_reasons=reasons,
    )


def detect(
    cs: CompleteSet, fees: Optional[FeeModel] = None, now: Optional[datetime] = None
) -> list[Opportunity]:
    """Run every detector against a complete set; return all opportunities found."""
    fees = fees or FeeModel()
    found = []
    for detector in (detect_buy_set, detect_mint_sell):
        op = detector(cs, fees, now)
        if op is not None:
            found.append(op)
    return found


def rank_key(op: Opportunity) -> tuple:
    """Sort key that puts *realistic* money on top, not the biggest paper edge.

    Feasible-under-the-$1-floor opportunities rank above infeasible ones; within
    each group we rank by net executable edge weighted by confidence, so a thin
    far-dated edge sinks below a smaller-but-real one. Falls back gracefully to
    ``total_edge`` when no realism layer ran (all-zero realism fields).
    """
    realism_value = op.net_total_edge * (op.confidence / 100.0)
    if op.confidence <= 0 and op.net_total_edge == 0:
        realism_value = op.total_edge  # no book-walk -> legacy ordering
    return (1 if op.feasible_min_order else 0, realism_value)


def scan_sets(
    sets, fees: Optional[FeeModel] = None, now: Optional[datetime] = None
) -> list[Opportunity]:
    """Detect across many complete sets, ranked by realistic (book-walked) edge."""
    fees = fees or FeeModel()
    out: list[Opportunity] = []
    for cs in sets:
        out.extend(detect(cs, fees, now))
    out.sort(key=rank_key, reverse=True)
    return out
