import unittest

from polymarket_arb.models import CompleteSet, Leg, Level
from polymarket_arb.odds_api import prices_by_team_from_events
from polymarket_arb.sports_value import (
    consensus_fair_probs,
    decimal_to_implied,
    devig,
    is_world_cup_market,
    normalize_team,
    scan_world_cup_value,
    world_cup_fair_value,
)


def wc_market(market_id, team, yes_ask, no_ask=None):
    no_ask = (1.0 - yes_ask + 0.02) if no_ask is None else no_ask
    return CompleteSet(
        market_id=market_id,
        question=f"Will {team} win the 2026 World Cup?",
        legs=[
            Leg("y", "Yes", Level(yes_ask, 1000), None),
            Leg("n", "No", Level(no_ask, 1000), None),
        ],
    )


class TestOddsMath(unittest.TestCase):
    def test_decimal_to_implied(self):
        self.assertAlmostEqual(decimal_to_implied(2.0), 0.5)
        self.assertAlmostEqual(decimal_to_implied(5.0), 0.2)
        self.assertIsNone(decimal_to_implied(1.0))   # no payout over stake
        self.assertIsNone(decimal_to_implied("x"))

    def test_devig_sums_to_one(self):
        out = devig({"a": 0.6, "b": 0.6})  # 1.2 overround
        self.assertAlmostEqual(sum(out.values()), 1.0)
        self.assertAlmostEqual(out["a"], 0.5)

    def test_consensus_averages_books_then_devigs(self):
        # Two books: Brazil 4.0/5.0 -> implied 0.25/0.20 -> avg 0.225
        #            Field   1.3/1.3 -> implied ~0.769 -> avg 0.769
        prices = {"Brazil": [4.0, 5.0], "Field": [1.3, 1.3]}
        fair = consensus_fair_probs(prices)
        self.assertAlmostEqual(sum(fair.values()), 1.0)
        self.assertLess(fair["brazil"], 0.25)  # de-vig pulls it below raw implied


class TestTeamMatching(unittest.TestCase):
    def test_normalize_aliases(self):
        self.assertEqual(normalize_team("USA"), "united states")
        self.assertEqual(normalize_team(" Korea "), "south korea")

    def test_is_world_cup_market(self):
        self.assertTrue(is_world_cup_market("Will Brazil win the World Cup?"))
        self.assertFalse(is_world_cup_market("Will it rain in NYC?"))

    def test_fair_value_matches_team_and_alias(self):
        sets = [
            wc_market("m-bra", "Brazil", 0.20),
            CompleteSet("m-usa", "Will the USA win the World Cup?",
                        legs=[Leg("y", "Yes", Level(0.05, 100), None),
                              Leg("n", "No", Level(0.96, 100), None)]),
        ]
        fair_by_team = {"brazil": 0.25, "united states": 0.04}
        fv = world_cup_fair_value(sets, fair_by_team)
        self.assertAlmostEqual(fv["m-bra"], 0.25)
        self.assertAlmostEqual(fv["m-usa"], 0.04)  # matched via USA->united states


class TestScanWorldCupValue(unittest.TestCase):
    def test_flags_underpriced_team(self):
        # Consensus fair ~0.25 for Brazil; Polymarket sells YES at 0.20 -> value.
        sets = [wc_market("m-bra", "Brazil", yes_ask=0.20, no_ask=0.82)]
        prices = {"Brazil": [4.0], "Field": [1.34]}  # implied 0.25 / 0.746
        ops = scan_world_cup_value(sets, prices, min_edge=0.02)
        yes = [o for o in ops if o.side == "YES"]
        self.assertTrue(yes)
        self.assertEqual(yes[0].market_id, "m-bra")
        self.assertGreater(yes[0].ev_per_contract, 0)

    def test_ignores_non_world_cup_markets(self):
        sets = [CompleteSet("x", "Will it rain?", legs=[
            Leg("y", "Yes", Level(0.2, 10), None),
            Leg("n", "No", Level(0.82, 10), None)])]
        self.assertEqual(scan_world_cup_value(sets, {"Brazil": [4.0]}), [])

    def test_no_match_no_signal(self):
        sets = [wc_market("m-arg", "Argentina", 0.20)]
        # consensus only has Brazil -> Argentina market gets no fair value.
        self.assertEqual(scan_world_cup_value(sets, {"Brazil": [4.0]}), [])


class TestOddsApiParser(unittest.TestCase):
    def test_flattens_events_to_prices(self):
        events = [{
            "bookmakers": [
                {"markets": [{"key": "outrights", "outcomes": [
                    {"name": "Brazil", "price": 5.5}, {"name": "France", "price": 6.0}]}]},
                {"markets": [{"key": "outrights", "outcomes": [
                    {"name": "Brazil", "price": 5.0}]}]},
            ]
        }]
        prices = prices_by_team_from_events(events, "outrights")
        self.assertEqual(sorted(prices["Brazil"]), [5.0, 5.5])
        self.assertEqual(prices["France"], [6.0])

    def test_skips_other_market_keys(self):
        events = [{"bookmakers": [{"markets": [
            {"key": "h2h", "outcomes": [{"name": "Brazil", "price": 2.0}]}]}]}]
        self.assertEqual(prices_by_team_from_events(events, "outrights"), {})


if __name__ == "__main__":
    unittest.main()
