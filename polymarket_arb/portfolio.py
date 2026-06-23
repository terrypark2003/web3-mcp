"""Bankroll allocation and bet sizing — turning detected edges into stakes.

This is where "numerous small bets" becomes a concrete plan. The module does
two distinct jobs that correspond to two distinct regimes:

1. Arbitrage sizing (``allocate_portfolio``)
   The scanner produces *near risk-free* opportunities. Textbook Kelly is
   degenerate for a riskless edge (it says "bet everything"), so the real
   limiters are:
       - order-book depth          (you can't fill more than exists)
       - a per-market cap           (bounds resolution / oracle tail risk)
       - the bankroll / total deployment cap
       - a minimum stake            (a fee/gas floor: tiny bets aren't worth it)
   Capital is spread greedily across the best risk-adjusted opportunities so
   no single market resolution can sink the book. Diversification only helps
   if the markets resolve *independently* — see the note in the summary.

2. Directional sizing (``kelly_fraction`` / ``kelly_stake``)
   For a bet with a genuine forecasting edge (your probability estimate beats
   the market's), the Kelly criterion gives the growth-optimal fraction.
   Fractional Kelly (e.g. half) is used in practice because it is far more
   robust to estimation error. Provided as a helper for bets the scanner does
   not generate.

Sizing only. Nothing here places an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import ARB_MINT_SELL, Opportunity


@dataclass
class SizingConfig:
    bankroll: float
    per_market_cap_frac: float = 0.05   # max fraction of bankroll on any one market
    min_stake: float = 1.0              # fee/gas floor: skip bets smaller than this
    max_deployed_frac: float = 1.0      # cap on total capital deployed at once


@dataclass
class Allocation:
    market_id: str
    question: str
    kind: str
    stake: float                # USDC deployed on this market
    sets: float                 # number of complete sets bought/minted
    expected_profit: float      # stake-scaled net edge
    edge_pct: float             # return on this stake, in percent
    annualized_pct: Optional[float]
    binding_constraint: str     # what capped this stake (depth / market-cap / bankroll)


@dataclass
class PortfolioSummary:
    bankroll: float
    total_deployed: float
    total_expected_profit: float
    blended_return_pct: float        # expected profit / capital deployed
    n_bets: int
    n_markets: int
    max_single_exposure_pct: float   # largest stake as a fraction of bankroll
    allocations: list[Allocation]


# --------------------------------------------------------------------------- #
# Directional (Kelly) sizing
# --------------------------------------------------------------------------- #

def kelly_fraction(win_prob: float, net_odds: float) -> float:
    """Full-Kelly fraction for a bet that pays ``net_odds``:1 with prob ``win_prob``.

    f* = p - (1 - p) / b, floored at 0 (never bet a -EV proposition).
    """
    if not 0.0 <= win_prob <= 1.0 or net_odds <= 0.0:
        return 0.0
    f = win_prob - (1.0 - win_prob) / net_odds
    return max(0.0, f)


def kelly_stake(
    bankroll: float,
    win_prob: float,
    net_odds: float,
    kelly_fraction_used: float = 0.5,
    cap_frac: float = 1.0,
) -> float:
    """Recommended stake for a directional bet, using fractional Kelly.

    ``kelly_fraction_used`` scales the full-Kelly fraction (0.5 = half-Kelly).
    ``cap_frac`` hard-caps the stake at a fraction of bankroll regardless of
    what Kelly suggests.
    """
    full = kelly_fraction(win_prob, net_odds)
    frac = min(full * kelly_fraction_used, cap_frac)
    return max(0.0, frac * bankroll)


# --------------------------------------------------------------------------- #
# Arbitrage portfolio allocation
# --------------------------------------------------------------------------- #

def _score(op: Opportunity) -> tuple:
    """Rank opportunities: instant (MINT_SELL) first, then by annualized return."""
    if op.kind == ARB_MINT_SELL:
        return (1, float("inf"))
    return (0, op.annualized_pct if op.annualized_pct is not None else op.edge_pct)


def allocate_portfolio(
    opportunities: list[Opportunity], cfg: SizingConfig
) -> PortfolioSummary:
    """Greedily spread bankroll across the best risk-adjusted arbitrage edges."""
    per_market_cap = cfg.per_market_cap_frac * cfg.bankroll
    remaining = cfg.bankroll * cfg.max_deployed_frac

    allocations: list[Allocation] = []
    for op in sorted(opportunities, key=_score, reverse=True):
        if remaining < cfg.min_stake:
            break

        # Candidate caps, smallest wins; track which one binds.
        caps = {
            "depth": op.capital_required,
            "market-cap": per_market_cap,
            "bankroll": remaining,
        }
        stake = min(caps.values())
        if stake < cfg.min_stake:
            continue
        binding = min(caps, key=caps.get)

        sets = stake / op.cost_per_set if op.cost_per_set > 0 else 0.0
        expected_profit = sets * op.edge_per_set

        allocations.append(
            Allocation(
                market_id=op.market_id,
                question=op.question,
                kind=op.kind,
                stake=stake,
                sets=sets,
                expected_profit=expected_profit,
                edge_pct=op.edge_pct,
                annualized_pct=op.annualized_pct,
                binding_constraint=binding,
            )
        )
        remaining -= stake

    total_deployed = sum(a.stake for a in allocations)
    total_expected = sum(a.expected_profit for a in allocations)
    blended = (total_expected / total_deployed * 100) if total_deployed > 0 else 0.0
    max_single = (
        max((a.stake for a in allocations), default=0.0) / cfg.bankroll * 100
        if cfg.bankroll > 0
        else 0.0
    )

    return PortfolioSummary(
        bankroll=cfg.bankroll,
        total_deployed=total_deployed,
        total_expected_profit=total_expected,
        blended_return_pct=blended,
        n_bets=len(allocations),
        n_markets=len({a.market_id for a in allocations}),
        max_single_exposure_pct=max_single,
        allocations=allocations,
    )


def format_portfolio(summary: PortfolioSummary) -> str:
    if not summary.allocations:
        return "No bets sized: no opportunities cleared the bankroll/cap/fee thresholds."

    header = (
        f"{'KIND':<10} {'STAKE$':>9} {'SETS':>9} {'EXP.PROFIT$':>12} "
        f"{'RET%':>6} {'CAP-BY':>11}  QUESTION"
    )
    lines = [header, "-" * len(header)]
    for a in summary.allocations:
        q = a.question if len(a.question) <= 40 else a.question[:37] + "..."
        lines.append(
            f"{a.kind:<10} {a.stake:>9.2f} {a.sets:>9.1f} {a.expected_profit:>12.2f} "
            f"{a.edge_pct:>5.2f}% {a.binding_constraint:>11}  {q}"
        )

    s = summary
    lines.append("-" * len(header))
    lines.append(
        f"Bankroll ${s.bankroll:,.2f} | deployed ${s.total_deployed:,.2f} "
        f"({s.total_deployed / s.bankroll * 100:.1f}%) across {s.n_bets} bets / "
        f"{s.n_markets} markets"
    )
    lines.append(
        f"Expected profit ${s.total_expected_profit:,.2f} "
        f"(blended {s.blended_return_pct:.2f}% on deployed) | "
        f"max single-market exposure {s.max_single_exposure_pct:.1f}% of bankroll"
    )
    lines.append(
        "Note: diversification reduces risk only if markets resolve "
        "independently; correlated bets are effectively one bet."
    )
    return "\n".join(lines)
