"""Depth-aware execution economics and a realism (confidence) score.

WHY THIS EXISTS
---------------
The detectors in ``detect.py`` answer "is there an edge at the *best* price?"
But the best price is often only a few shares deep, and Polymarket's
$1-minimum-order floor means you frequently cannot take even that without
walking into worse prices that erase the edge. That is exactly why the
OpenAI-IPO alert showed a "$0.19 edge" that was not real money.

This module answers the harder, realistic questions:

* How many sets can I actually buy/sell while the per-set edge survives a walk
  down the order book, net of fees and gas?  (``executable_buy_set`` /
  ``executable_mint_sell``)
* Given that, how much should I *trust* this opportunity — depth headroom over
  the $1 floor, how long capital is locked, how many legs must all fill, and
  whether the net edge leaves any slippage buffer?  (``confidence_score``)

The score lets the scanner rank real money above noise instead of the other
way around: a thin, far-dated, barely-positive edge scores near zero even if
its top-of-book ``edge_pct`` looks juicy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .models import Leg, Level


# --------------------------------------------------------------------------- #
# Order-book ladders
# --------------------------------------------------------------------------- #

def ask_ladder(leg: Leg) -> list[Level]:
    """Ask levels cheapest-first. Falls back to top-of-book when no full ladder."""
    if leg.asks:
        return sorted(leg.asks, key=lambda lv: lv.price)
    return [leg.best_ask] if leg.best_ask is not None else []


def bid_ladder(leg: Leg) -> list[Level]:
    """Bid levels best (highest) first. Falls back to top-of-book."""
    if leg.bids:
        return sorted(leg.bids, key=lambda lv: lv.price, reverse=True)
    return [leg.best_bid] if leg.best_bid is not None else []


def walk_buy_cost(asks: list[Level], shares: float) -> Optional[float]:
    """USDC to BUY ``shares`` walking ``asks`` cheapest-first.

    Returns None if the ladder is too thin to fill ``shares`` — an honest
    "you cannot get this size", not a silently truncated fill.
    """
    if shares <= 0:
        return 0.0
    remaining = shares
    cost = 0.0
    for lv in sorted(asks, key=lambda x: x.price):
        take = min(remaining, lv.size)
        cost += take * lv.price
        remaining -= take
        if remaining <= 1e-9:
            return cost
    return None


def walk_sell_proceeds(bids: list[Level], shares: float) -> Optional[float]:
    """USDC received SELLING ``shares`` into ``bids`` best-first. None if too thin."""
    if shares <= 0:
        return 0.0
    remaining = shares
    proceeds = 0.0
    for lv in sorted(bids, key=lambda x: x.price, reverse=True):
        take = min(remaining, lv.size)
        proceeds += take * lv.price
        remaining -= take
        if remaining <= 1e-9:
            return proceeds
    return None


# --------------------------------------------------------------------------- #
# Executable size (book-walked)
# --------------------------------------------------------------------------- #

@dataclass
class Executable:
    """The realistic, book-walked outcome of sizing an opportunity."""

    executable_sets: float        # max sets while edge >= min_edge_per_set
    edge_per_set: float           # realized per-set edge at that size (gross of gas)
    net_total_edge: float         # edge_per_set*sets, minus amortized gas
    min_order_shares: float       # K: shares the $1-per-order floor forces
    feasible_min_order: bool      # executable_sets >= K (can actually be placed)
    total_depth: float            # min across legs of total ladder depth
    reasons: list[str] = field(default_factory=list)


def _max_size_above_edge(
    cost_or_proceeds, depth: float, min_edge_per_set: float, is_buy: bool
) -> float:
    """Largest size in (0, depth] whose per-set edge >= ``min_edge_per_set``.

    ``cost_or_proceeds(s)`` returns the *per-set* cost (buy) or proceeds (sell),
    which is monotone in ``s`` as you walk the book, so the per-set edge is
    monotone decreasing — a clean binary search applies.
    """
    if depth <= 0:
        return 0.0

    def edge(s: float) -> Optional[float]:
        v = cost_or_proceeds(s)
        if v is None:
            return None
        return (1.0 - v) if is_buy else (v - 1.0)

    e_top = edge(min(depth, 1e-6))
    if e_top is None or e_top < min_edge_per_set:
        # Even an infinitesimal size doesn't clear the bar.
        e_full = edge(depth)
        if e_full is None or e_full < min_edge_per_set:
            return 0.0
    lo, hi = 0.0, depth
    for _ in range(40):  # ~1e-12 relative precision over the depth range
        mid = (lo + hi) / 2
        e = edge(mid)
        if e is not None and e >= min_edge_per_set:
            lo = mid
        else:
            hi = mid
    return lo


def executable_buy_set(
    legs: list[Leg],
    *,
    taker_fee_rate: float = 0.0,
    gas_cost_usd: float = 0.0,
    min_edge_per_set: float = 0.005,
    min_order_usd: float = 1.0,
) -> Optional[Executable]:
    """Walk every leg's ask ladder to size a BUY_SET realistically."""
    ladders = [ask_ladder(leg) for leg in legs]
    if not ladders or any(not a for a in ladders):
        return None
    total_depth = min(sum(lv.size for lv in a) for a in ladders)
    if total_depth <= 0:
        return None

    def cost_per_set(s: float) -> Optional[float]:
        total = 0.0
        for a in ladders:
            c = walk_buy_cost(a, s)
            if c is None:
                return None
            total += c
        per = total / s
        return per + taker_fee_rate * per  # fee scales with notional

    sets = _max_size_above_edge(cost_per_set, total_depth, min_edge_per_set, is_buy=True)

    cheapest = min(a[0].price for a in ladders)
    k_floor = math.ceil(min_order_usd / cheapest) if cheapest > 0 else math.inf

    if sets <= 0:
        return Executable(0.0, 0.0, 0.0, k_floor, False, total_depth,
                          ["호가를 한 단계만 내려가도 차익 소멸"])
    cps = cost_per_set(sets) or 0.0
    edge_per_set = 1.0 - cps
    net_total = edge_per_set * sets - gas_cost_usd
    feasible = sets >= k_floor - 1e-9
    reasons: list[str] = []
    if not feasible:
        reasons.append(
            f"$1 최소주문에 {int(k_floor)}주 필요하나 차익 유지 깊이는 {sets:.0f}주뿐"
        )
    return Executable(sets, edge_per_set, net_total, k_floor, feasible, total_depth, reasons)


def executable_mint_sell(
    legs: list[Leg],
    *,
    taker_fee_rate: float = 0.0,
    gas_cost_usd: float = 0.0,
    min_edge_per_set: float = 0.005,
    min_order_usd: float = 1.0,
) -> Optional[Executable]:
    """Walk every leg's bid ladder to size a MINT_SELL realistically.

    Mint a set for $1, then sell each leg into the bids. The $1-per-order floor
    binds on the *cheapest* leg's top bid (the one that struggles to clear $1).
    """
    ladders = [bid_ladder(leg) for leg in legs]
    if not ladders or any(not b for b in ladders):
        return None
    total_depth = min(sum(lv.size for lv in b) for b in ladders)
    if total_depth <= 0:
        return None

    def proceeds_per_set(s: float) -> Optional[float]:
        total = 0.0
        for b in ladders:
            p = walk_sell_proceeds(b, s)
            if p is None:
                return None
            total += p
        per = total / s
        return per - taker_fee_rate * per

    sets = _max_size_above_edge(proceeds_per_set, total_depth, min_edge_per_set, is_buy=False)

    cheapest = min(b[0].price for b in ladders)
    k_floor = math.ceil(min_order_usd / cheapest) if cheapest > 0 else math.inf

    if sets <= 0:
        return Executable(0.0, 0.0, 0.0, k_floor, False, total_depth,
                          ["호가를 한 단계만 올라가도 차익 소멸"])
    pps = proceeds_per_set(sets) or 0.0
    edge_per_set = pps - 1.0
    net_total = edge_per_set * sets - gas_cost_usd
    feasible = sets >= k_floor - 1e-9
    reasons: list[str] = []
    if not feasible:
        reasons.append(
            f"$1 최소주문에 {int(k_floor)}주 필요하나 차익 유지 깊이는 {sets:.0f}주뿐"
        )
    return Executable(sets, edge_per_set, net_total, k_floor, feasible, total_depth, reasons)


# --------------------------------------------------------------------------- #
# Confidence score
# --------------------------------------------------------------------------- #

def confidence_score(
    ex: Executable,
    *,
    instant: bool,
    years: Optional[float],
    n_legs: int,
) -> tuple[float, list[str]]:
    """Fold realism factors into a 0-100 score (higher = more likely real money).

    The factors are multiplicative 0-1 sub-scores, each capturing one reason a
    paper edge fails to become cash:

    * feasibility/headroom — can the $1-per-order floor even be met, and with
      how much room above it? An infeasible edge is capped hard (it cannot be
      placed), and depth only 1x the floor scores far below 5x+ depth.
    * lockup — held capital that's stuck for months is worth less than instant;
      an unknown resolution date is penalized for the uncertainty.
    * leg count — every extra leg is one more order that must fill for the hedge
      to hold, so execution risk grows with legs.
    * slippage buffer — a fat net edge tolerates a missed tick; a 0.1¢ edge does
      not, so tiny edges are discounted even when technically feasible.
    """
    reasons: list[str] = list(ex.reasons)

    # 1) $1-floor feasibility + depth headroom.
    if not ex.feasible_min_order or ex.executable_sets <= 0:
        feas = 0.12
        if not any("최소주문" in r for r in reasons):
            reasons.append(
                f"$1 최소주문에 {int(ex.min_order_shares)}주 필요하나 "
                f"차익 유지 깊이는 {ex.executable_sets:.0f}주뿐 — 실행 어려움"
            )
    else:
        headroom = ex.executable_sets / ex.min_order_shares if ex.min_order_shares else 5.0
        # 1x floor -> 0.5, 5x or more -> 1.0.
        feas = 0.5 + 0.5 * min(1.0, (headroom - 1.0) / 4.0)
        reasons.append(
            f"깊이 {ex.executable_sets:.0f}주 = 최소주문 {int(ex.min_order_shares)}주의 "
            f"{headroom:.1f}배"
        )

    # 2) Capital lockup.
    if instant:
        lock = 1.0
        reasons.append("즉시 정산 (자본 안 묶임)")
    elif years is None:
        lock = 0.6
        reasons.append("정산일 불명 — 잠김기간 미상")
    else:
        lock = 1.0 / (1.0 + max(0.0, years) * 2.0)  # 0y->1, ~0.5y->0.5, 1y->0.33
        reasons.append(f"자본 잠김 ~{max(0.0, years) * 365:.0f}일")

    # 3) Leg / execution risk.
    legrisk = 1.0 / (1.0 + 0.08 * max(0, n_legs - 2))

    # 4) Slippage buffer from the net edge (>= 2¢/set counts as full buffer).
    buf = min(1.0, max(0.0, ex.edge_per_set) / 0.02)
    buffer_factor = 0.4 + 0.6 * buf

    score = 100.0 * feas * lock * legrisk * buffer_factor
    return round(max(0.0, min(100.0, score)), 1), reasons
