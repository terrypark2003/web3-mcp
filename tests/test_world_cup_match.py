import unittest

from polymarket_arb.models import CompleteSet, Leg, Level
from polymarket_arb.odds_api import match_prices_from_events
from polymarket_arb.sports_value import scan_world_cup_match_value


def poly_match(question, legs, end_date="2026-06-30T20:00:00Z"):
    return CompleteSet(
        market_id=question,
        question=question,
        legs=[Leg(f"t-{o}", o, Level(ask, size), None) for o, ask, size in legs],
        end_date=end_date,
        venue="polymarket",
        url="https://polymarket.com/event/x",
    )


# Brazil 2.0 / Draw 4.0 / Chile 4.0 -> implied .5/.25/.25, sum 1.0 (no vig)
NO_VIG_MATCH = [{
    "home": "Brazil", "away": "Chile", "commence_time": "2026-06-30T18:00:00Z",
    "prices": {"Brazil": [2.0, 2.0], "Draw": [4.0, 4.0], "Chile": [4.0, 4.0]},
}]


class TestMatchPricesParser(unittest.TestCase):
    def test_parses_event_into_price_table(self):
        events = [{
            "home_team": "Brazil", "away_team": "Chile",
            "commence_time": "2026-06-30T18:00:00Z",
            "bookmakers": [
                {"markets": [{"key": "h2h", "outcomes": [
                    {"name": "Brazil", "price": 1.8}, {"name": "Draw", "price": 3.5},
                    {"name": "Chile", "price": 4.5}]}]},
                {"markets": [{"key": "h2h", "outcomes": [
                    {"name": "Brazil", "price": 1.9}, {"name": "Draw", "price": 3.4},
                    {"name": "Chile", "price": 4.2}]}]},
            ],
        }]
        out = match_prices_from_events(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["home"], "Brazil")
        self.assertEqual(out[0]["prices"]["Brazil"], [1.8, 1.9])
        self.assertEqual(out[0]["prices"]["Draw"], [3.5, 3.4])

    def test_ignores_non_h2h_markets(self):
        events = [{"home_team": "A", "away_team": "B", "bookmakers": [
            {"markets": [{"key": "totals", "outcomes": [{"name": "Over", "price": 2.0}]}]}]}]
        self.assertEqual(match_prices_from_events(events), [])


class TestMatchValueDetector(unittest.TestCase):
    def test_finds_team_value_above_threshold(self):
        # Brazil fair .5, ask .40 -> EV +.10; Draw/Chile not value.
        sets = [poly_match("Brazil vs. Chile (2026 World Cup)",
                           [("Brazil", 0.40, 100), ("Draw", 0.30, 100), ("Chile", 0.30, 100)])]
        ops = scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.05)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].side, "Brazil")
        self.assertAlmostEqual(ops[0].fair_prob, 0.5, places=3)
        self.assertAlmostEqual(ops[0].ev_per_contract, 0.10, places=3)

    def test_draw_outcome_supported(self):
        sets = [poly_match("Brazil vs. Chile",
                           [("Brazil", 0.55, 100), ("Draw", 0.18, 100), ("Chile", 0.30, 100)])]
        ops = scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.05)
        sides = {o.side for o in ops}
        self.assertIn("Draw", sides)  # fair .25 vs ask .18 -> +.07

    def test_no_value_when_teams_not_in_question(self):
        sets = [poly_match("Spain vs. Italy",
                           [("Spain", 0.10, 100), ("Draw", 0.10, 100), ("Italy", 0.10, 100)])]
        self.assertEqual(scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.05), [])

    def test_yes_no_legs_are_skipped(self):
        # A binary "Will Brazil win?" can't be resolved to a team reliably -> skip.
        sets = [poly_match("Brazil vs. Chile — will Brazil win?",
                           [("Yes", 0.30, 100), ("No", 0.30, 100)])]
        self.assertEqual(scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.05), [])

    def test_min_edge_filters(self):
        # Chile fair .25 vs ask .22 -> +.03: out at .05, in at .02.
        sets = [poly_match("Brazil vs. Chile",
                           [("Brazil", 0.49, 100), ("Draw", 0.27, 100), ("Chile", 0.22, 100)])]
        self.assertEqual(scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.05), [])
        ops = scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.02)
        self.assertEqual({o.side for o in ops}, {"Chile"})

    def test_devig_removes_overround(self):
        # Heavy vig: implied sums > 1; fair must be normalized before comparing.
        match = [{"home": "Brazil", "away": "Chile",
                  "prices": {"Brazil": [1.5], "Draw": [3.0], "Chile": [4.0]}}]
        sets = [poly_match("Brazil vs. Chile", [("Brazil", 0.50, 100)])]
        ops = scan_world_cup_match_value(sets, match, min_edge=0.01)
        # raw implied Brazil = .667, but de-vigged is lower; fair must be < .667.
        self.assertTrue(ops)
        self.assertLess(ops[0].fair_prob, 0.667)

    def test_thin_depth_below_min_size_skipped(self):
        sets = [poly_match("Brazil vs. Chile", [("Brazil", 0.40, 0.5)])]
        self.assertEqual(scan_world_cup_match_value(sets, NO_VIG_MATCH, min_edge=0.05, min_size=1.0), [])


class TestDemoAndNotify(unittest.TestCase):
    def test_demo_loader_yields_match_value(self):
        from polymarket_arb.demo import load_demo_world_cup_matches
        ops = load_demo_world_cup_matches(min_edge=0.05)
        self.assertTrue(ops)
        self.assertEqual(ops[0].side, "Argentina")  # the seeded +6.6% value bet

    def test_build_payload_demo_is_per_match(self):
        from polymarket_arb.notify import build_world_cup_payload
        payload = build_world_cup_payload(demo=True)
        self.assertEqual(payload["meta"]["source"], "demo")
        self.assertTrue(payload["world_cup"])
        # Per-match side is a team name, not the outright "Yes"/"No".
        self.assertEqual(payload["world_cup"][0]["side"], "Argentina")


if __name__ == "__main__":
    unittest.main()
