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

from .ev import EV_SIGNAL, EVOpportunity, fair_value_from_map, scan_ev
from .models import CompleteSet

WORLD_CUP_KEYWORDS = ("world cup",)
DRAW_FORMS = {"draw", "tie", "x"}  # how a drawn-match outcome is labelled

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


# --------------------------------------------------------------------------- #
# Per-match value (h2h) — the near-dated "games in the next day or two" path
# --------------------------------------------------------------------------- #
#
# The outright path above is tournament-long (settles at the final). For a
# market that resolves within a day or two you need per-MATCH value: take a
# match's home/draw/away bookmaker odds, de-vig them into fair probabilities,
# and compare each to the Polymarket price for that same outcome.


def _team_in_question(question: str, team_norm: str) -> bool:
    """Does any surface form of ``team_norm`` appear in the market question?"""
    return any(form in question for form in _surface_forms(team_norm))


def _leg_fair_prob(
    outcome: str, home_norm: str, away_norm: str, fair: dict[str, float]
) -> Optional[float]:
    """Fair probability for one Polymarket leg of a match market, or None.

    Handles team-named legs (moneyline / 3-way) and an explicit Draw. ``Yes``/
    ``No`` legs are skipped — which team a binary "Will X win?" refers to can't
    be resolved reliably when both teams are named, so we don't guess.
    """
    o = normalize_team(outcome)
    if o in DRAW_FORMS:
        return fair.get("draw")
    if o == home_norm:
        return fair.get(home_norm)
    if o == away_norm:
        return fair.get(away_norm)
    return None


def scan_world_cup_match_value(
    poly_sets,
    matches: list,
    min_edge: float = 0.05,
    min_size: float = 1.0,
) -> list[EVOpportunity]:
    """Positive-value per-match bets: Polymarket price vs de-vigged match odds.

    ``poly_sets`` are Polymarket per-match markets (legs labelled by team or
    Draw). ``matches`` is the odds-API output from ``match_prices_from_events``:
    one entry per game with ``home``/``away`` and a ``prices`` table. A set is
    paired to a match when both team names appear in its question; then each leg
    whose fair probability beats its ask by ``min_edge`` is surfaced.
    """
    parsed = []
    for m in matches:
        fair = consensus_fair_probs(m.get("prices", {}))  # de-vig within the match
        if not fair:
            continue
        parsed.append((
            normalize_team(m.get("home", "")),
            normalize_team(m.get("away", "")),
            fair,
        ))

    out: list[EVOpportunity] = []
    for cs in poly_sets:
        q = (cs.question or "").lower()
        pair = next(
            (
                (home, away, fair)
                for home, away, fair in parsed
                if home and away
                and _team_in_question(q, home) and _team_in_question(q, away)
            ),
            None,
        )
        if pair is None:
            continue
        home, away, fair = pair
        for leg in cs.legs:
            if leg.best_ask is None or leg.best_ask.size < min_size:
                continue
            p = _leg_fair_prob(leg.outcome, home, away, fair)
            if p is None:
                continue
            ev = p - leg.best_ask.price
            if ev < min_edge:
                continue
            out.append(EVOpportunity(
                kind=EV_SIGNAL,
                market_id=cs.market_id,
                question=cs.question,
                venue=cs.venue,
                side=leg.outcome,
                price=leg.best_ask.price,
                fair_prob=p,
                ev_per_contract=ev,
                edge_pct=(ev / leg.best_ask.price * 100) if leg.best_ask.price > 0 else 0.0,
                max_size=leg.best_ask.size,
                end_date=cs.end_date,
                url=cs.url,
            ))
    out.sort(key=lambda o: o.ev_per_contract, reverse=True)
    return out
