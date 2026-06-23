"""Sports value betting: Polymarket price vs de-vigged bookmaker consensus.

"Good odds" only means something against a benchmark of the *true* probability.
For sports, the standard benchmark is the bookmaker consensus: average the
implied probabilities across many books, then remove the overround ("de-vig")
so they sum to 1. If Polymarket lets you buy an outcome below that fair
probability, it's a positive-EV ("good odds") bet.

This is NOT arbitrage — it's an opinion that the consensus is closer to the
truth than Polymarket's price. Any single bet can lose in full; size with
fractional Kelly.

The actual value math reuses ``ev.py``: this module only (a) turns raw
bookmaker odds into a fair-probability-per-team map, and (b) matches those
teams to the Polymarket "Will <team> win?" markets. Everything else is the
existing EV detector.
"""

from __future__ import annotations

from typing import Optional

from .ev import EVOpportunity, fair_value_from_map, scan_ev
from .models import CompleteSet

WORLD_CUP_KEYWORDS = ("world cup",)

# Surface form -> canonical team name. Best-effort; extend as needed.
_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "korea": "south korea",
    "republic of korea": "south korea",
    "iran": "ir iran",
    "south korea": "south korea",
    "england": "england",
    "the netherlands": "netherlands",
    "holland": "netherlands",
}


def normalize_team(name: str) -> str:
    n = " ".join((name or "").strip().lower().split())
    return _ALIASES.get(n, n)


def _surface_forms(canonical: str) -> set[str]:
    """All strings that should match a canonical team name in free text."""
    forms = {canonical}
    forms.update(s for s, c in _ALIASES.items() if c == canonical)
    return forms


def is_world_cup_market(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in WORLD_CUP_KEYWORDS)


def decimal_to_implied(odds) -> Optional[float]:
    """Decimal odds (e.g. 5.5) -> implied probability (1/odds). None if invalid."""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    return 1.0 / o if o > 1.0 else None


def consensus_implied(prices_by_team: dict[str, list]) -> dict[str, float]:
    """Average implied probability across books, per (normalized) team."""
    out: dict[str, float] = {}
    for team, prices in prices_by_team.items():
        imps = [decimal_to_implied(p) for p in prices]
        imps = [i for i in imps if i is not None]
        if imps:
            out[normalize_team(team)] = sum(imps) / len(imps)
    return out


def devig(implied: dict[str, float]) -> dict[str, float]:
    """Normalize implied probabilities to sum to 1 (remove the overround)."""
    total = sum(implied.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in implied.items()}


def consensus_fair_probs(prices_by_team: dict[str, list]) -> dict[str, float]:
    """Bookmaker odds -> de-vigged fair P(win) per normalized team."""
    return devig(consensus_implied(prices_by_team))


def world_cup_fair_value(
    poly_sets, fair_by_team: dict[str, float]
) -> dict[str, float]:
    """Map each Polymarket 'Will <team> win?' market_id -> consensus fair prob.

    Matches by finding a team (or its alias) named in the market question.
    """
    out: dict[str, float] = {}
    for cs in poly_sets:
        q = (cs.question or "").lower()
        for team_norm, prob in fair_by_team.items():
            if any(form in q for form in _surface_forms(team_norm)):
                out[cs.market_id] = prob
                break
    return out


def scan_world_cup_value(
    poly_sets,
    prices_by_team: dict[str, list],
    min_edge: float = 0.03,
    min_size: float = 1.0,
) -> list[EVOpportunity]:
    """Positive-EV World Cup bets: Polymarket price vs bookmaker consensus.

    ``poly_sets`` are binary Polymarket markets; only World Cup ones are kept.
    ``prices_by_team`` is {team: [decimal_odds_per_book, ...]} from the odds API.
    """
    fair_by_team = consensus_fair_probs(prices_by_team)
    wc = [
        cs for cs in poly_sets
        if is_world_cup_market(cs.question) and len(cs.legs) == 2
    ]
    fair = fair_value_from_map(world_cup_fair_value(wc, fair_by_team))
    return scan_ev(wc, fair, fees=None, min_ev=min_edge, min_size=min_size)
