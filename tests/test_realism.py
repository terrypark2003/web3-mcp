import unittest

from polymarket_arb.detect import FeeModel, detect_buy_set, scan_sets
from polymarket_arb.models import CompleteSet, Leg, Level
from polymarket_arb.realism import (
    Executable,
    ask_ladder,
    bid_ladder,
    confidence_score,
    executable_buy_set,
    executable_mint_sell,
    walk_buy_cost,
    walk_sell_proceeds,
)


def leg(outcome, asks=None, bids=None):
    """Leg with full ladders; top-of-book derived from the best level."""
    asks = [Level(p, s) for p, s in (asks or [])]
    bids = [Level(p, s) for p, s in (bids or [])]
    best_ask = min(asks, key=lambda x: x.price) if asks else None
    best_bid = max(bids, key=lambda x: x.price) if bids else None
    return Leg("t-" + outcome, outcome, best_ask, best_bid, asks=asks, bids=bids)


class TestBookWalk(unittest.TestCase):
    def test_walk_buy_cost_across_levels(self):
        asks = [Level(0.60, 5), Level(0.70, 100)]
        self.assertAlmostEqual(walk_buy_cost(asks, 5), 3.0)          # 5 @ .60
        self.assertAlmostEqual(walk_buy_cost(asks, 6), 3.0 + 0.70)   # +1 @ .70

    def test_walk_buy_cost_insufficient_depth_is_none(self):
        self.assertIsNone(walk_buy_cost([Level(0.5, 3)], 10))

    def test_walk_buy_cost_zero_shares(self):
        self.assertEqual(walk_buy_cost([Level(0.5, 3)], 0), 0.0)

    def test_walk_sell_proceeds_best_first(self):
        bids = [Level(0.40, 5), Level(0.30, 100)]
        self.assertAlmostEqual(walk_sell_proceeds(bids, 5), 2.0)         # 5 @ .40
        self.assertAlmostEqual(walk_sell_proceeds(bids, 6), 2.0 + 0.30)  # +1 @ .30

    def test_ladder_falls_back_to_top_of_book(self):
        lg = Leg("t", "Yes", Level(0.6, 10), Level(0.5, 8))  # no asks/bids lists
        self.assertEqual([lv.price for lv in ask_ladder(lg)], [0.6])
        self.assertEqual([lv.price for lv in bid_ladder(lg)], [0.5])


class TestExecutableBuySet(unittest.TestCase):
    def test_deep_liquid_is_fully_executable(self):
        legs = [leg("Yes", asks=[(0.60, 1000)]), leg("No", asks=[(0.38, 1000)])]
        ex = executable_buy_set(legs)
        self.assertAlmostEqual(ex.executable_sets, 1000, delta=1)
        self.assertAlmostEqual(ex.edge_per_set, 0.02, places=4)
        self.assertTrue(ex.feasible_min_order)          # 1000 >> ceil(1/0.38)=3
        self.assertGreater(ex.net_total_edge, 19)        # ~0.02 * 1000

    def test_edge_erodes_when_top_is_thin(self):
        # Edge exists only for the first ~5 shares; deeper levels kill it.
        legs = [
            leg("Yes", asks=[(0.60, 5), (0.70, 1000)]),
            leg("No", asks=[(0.38, 5), (0.40, 1000)]),
        ]
        ex = executable_buy_set(legs)
        # Break-even ~5.7 sets (computed by hand), not the 1000 of deep depth.
        self.assertGreater(ex.executable_sets, 5.0)
        self.assertLess(ex.executable_sets, 6.0)

    def test_dollar_floor_infeasible_openai_shape(self):
        # Cheapest leg 1.7c needs ceil(1/0.017)=59 shares to clear the $1 floor,
        # but only 5 shares of depth exist -> not actually executable.
        legs = [leg("<500B", asks=[(0.017, 5)]), leg("rest", asks=[(0.945, 5)])]
        ex = executable_buy_set(legs)
        self.assertEqual(ex.min_order_shares, 59)
        self.assertFalse(ex.feasible_min_order)

    def test_gas_reduces_net_edge(self):
        legs = [leg("Yes", asks=[(0.60, 100)]), leg("No", asks=[(0.38, 100)])]
        no_gas = executable_buy_set(legs, gas_cost_usd=0.0)
        with_gas = executable_buy_set(legs, gas_cost_usd=0.5)
        self.assertAlmostEqual(no_gas.net_total_edge - with_gas.net_total_edge, 0.5, places=6)

    def test_no_asks_returns_none(self):
        self.assertIsNone(executable_buy_set([leg("Yes"), leg("No")]))


class TestExecutableMintSell(unittest.TestCase):
    def test_proceeds_over_one_is_executable(self):
        # Sell both legs into bids summing > $1 -> instant edge.
        legs = [leg("Yes", bids=[(0.55, 100)]), leg("No", bids=[(0.50, 100)])]
        ex = executable_mint_sell(legs)
        self.assertAlmostEqual(ex.edge_per_set, 0.05, places=4)
        self.assertTrue(ex.feasible_min_order)


class TestConfidenceScore(unittest.TestCase):
    def _ex(self, sets, k, feasible, edge=0.03):
        return Executable(sets, edge, edge * sets, k, feasible, sets)

    def test_instant_beats_far_dated_held(self):
        ex = self._ex(1000, 3, True)
        instant, _ = confidence_score(ex, instant=True, years=None, n_legs=2)
        held, _ = confidence_score(ex, instant=False, years=1.0, n_legs=2)
        self.assertGreater(instant, held)

    def test_infeasible_floor_scores_low(self):
        ex = self._ex(5, 59, False)
        score, reasons = confidence_score(ex, instant=False, years=0.1, n_legs=8)
        self.assertLess(score, 20)
        self.assertTrue(any("최소주문" in r for r in reasons))

    def test_deep_short_fat_scores_high(self):
        ex = self._ex(1000, 3, True, edge=0.05)
        score, _ = confidence_score(ex, instant=True, years=None, n_legs=2)
        self.assertGreater(score, 80)

    def test_more_legs_lowers_score(self):
        ex = self._ex(1000, 3, True)
        few, _ = confidence_score(ex, instant=True, years=None, n_legs=2)
        many, _ = confidence_score(ex, instant=True, years=None, n_legs=12)
        self.assertGreater(few, many)


class TestDetectIntegration(unittest.TestCase):
    def _cs(self, market_id, legs, **kw):
        return CompleteSet(market_id, market_id, legs, **kw)

    def test_detect_populates_realism_fields(self):
        cs = self._cs("deep", [leg("Yes", asks=[(0.60, 1000)]), leg("No", asks=[(0.38, 1000)])])
        op = detect_buy_set(cs, FeeModel())
        self.assertGreater(op.confidence, 0)
        self.assertGreater(op.executable_sets, 100)
        self.assertTrue(op.feasible_min_order)

    def test_scan_ranks_feasible_above_paper_edge(self):
        # A: big paper edge but $1-floor-infeasible (thin, ultra-cheap leg).
        a = self._cs("paper", [leg("cheap", asks=[(0.017, 5)]), leg("rest", asks=[(0.90, 5)])])
        # B: smaller edge but deep and feasible.
        b = self._cs("real", [leg("Yes", asks=[(0.60, 1000)]), leg("No", asks=[(0.38, 1000)])])
        ranked = scan_sets([a, b], FeeModel())
        self.assertEqual(ranked[0].market_id, "real")     # real money on top
        self.assertFalse(ranked[-1].feasible_min_order)    # paper edge sinks


if __name__ == "__main__":
    unittest.main()
