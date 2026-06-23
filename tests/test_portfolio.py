import unittest

from polymarket_arb.models import ARB_BUY_SET, ARB_MINT_SELL, Opportunity
from polymarket_arb.portfolio import (
    SizingConfig,
    allocate_portfolio,
    kelly_fraction,
    kelly_stake,
)


def op(kind, market_id, cost_per_set, edge_per_set, max_sets, annualized=None):
    return Opportunity(
        kind=kind,
        market_id=market_id,
        question=f"q-{market_id}",
        neg_risk=False,
        exhaustive=True,
        end_date=None,
        n_legs=2,
        cost_per_set=cost_per_set,
        proceeds_per_set=1.0,
        edge_per_set=edge_per_set,
        edge_pct=edge_per_set / cost_per_set * 100,
        max_sets=max_sets,
        capital_required=cost_per_set * max_sets,
        total_edge=edge_per_set * max_sets,
        annualized_pct=annualized,
        legs=[],
    )


class TestKelly(unittest.TestCase):
    def test_even_money_positive_edge(self):
        # p=0.6 at even money -> f* = 0.6 - 0.4/1 = 0.2
        self.assertAlmostEqual(kelly_fraction(0.6, 1.0), 0.2, places=6)

    def test_negative_edge_floors_at_zero(self):
        self.assertEqual(kelly_fraction(0.4, 1.0), 0.0)

    def test_invalid_inputs(self):
        self.assertEqual(kelly_fraction(1.5, 1.0), 0.0)
        self.assertEqual(kelly_fraction(0.6, 0.0), 0.0)

    def test_half_kelly_stake(self):
        # full kelly 0.2 -> half 0.1 -> $100 of $1000
        self.assertAlmostEqual(kelly_stake(1000, 0.6, 1.0, 0.5), 100.0, places=6)

    def test_stake_cap(self):
        # half-kelly suggests 0.1; cap at 0.05 -> $50
        self.assertAlmostEqual(
            kelly_stake(1000, 0.6, 1.0, 0.5, cap_frac=0.05), 50.0, places=6
        )


class TestAllocate(unittest.TestCase):
    def setUp(self):
        self.ops = [
            op(ARB_BUY_SET, "m1", 0.98, 0.02, 90, annualized=55.0),
            op(ARB_MINT_SELL, "m2", 1.0, 0.05, 250),
        ]

    def test_instant_ranked_first(self):
        s = allocate_portfolio(self.ops, SizingConfig(bankroll=1000))
        self.assertEqual(s.allocations[0].kind, ARB_MINT_SELL)

    def test_market_cap_binds(self):
        s = allocate_portfolio(
            self.ops, SizingConfig(bankroll=1000, per_market_cap_frac=0.05)
        )
        # Each capped at 5% of 1000 = $50.
        for a in s.allocations:
            self.assertAlmostEqual(a.stake, 50.0, places=6)
            self.assertEqual(a.binding_constraint, "market-cap")
        self.assertAlmostEqual(s.total_deployed, 100.0, places=6)
        self.assertAlmostEqual(s.max_single_exposure_pct, 5.0, places=6)
        # MINT_SELL: 50 sets * 0.05 edge = 2.5 expected profit.
        self.assertAlmostEqual(s.allocations[0].expected_profit, 2.5, places=6)

    def test_depth_binds(self):
        s = allocate_portfolio(
            self.ops, SizingConfig(bankroll=1000, per_market_cap_frac=0.5)
        )
        by_market = {a.market_id: a for a in s.allocations}
        # m2 depth = 1.0 * 250 = 250; m1 depth = 0.98 * 90 = 88.2
        self.assertAlmostEqual(by_market["m2"].stake, 250.0, places=6)
        self.assertEqual(by_market["m2"].binding_constraint, "depth")
        self.assertAlmostEqual(by_market["m1"].stake, 88.2, places=6)
        self.assertEqual(by_market["m1"].binding_constraint, "depth")

    def test_bankroll_binds_last_bet(self):
        s = allocate_portfolio(
            self.ops, SizingConfig(bankroll=300, per_market_cap_frac=0.9)
        )
        # m2 (instant) takes its depth 250 first; m1 then limited to remaining 50.
        self.assertAlmostEqual(s.total_deployed, 300.0, places=6)
        last = s.allocations[-1]
        self.assertAlmostEqual(last.stake, 50.0, places=6)
        self.assertEqual(last.binding_constraint, "bankroll")

    def test_min_stake_filters_everything(self):
        s = allocate_portfolio(
            self.ops, SizingConfig(bankroll=10, per_market_cap_frac=0.05, min_stake=1.0)
        )
        # caps are $0.50 < $1 min stake -> nothing sized.
        self.assertEqual(s.allocations, [])
        self.assertEqual(s.n_bets, 0)

    def test_max_deployed_caps_total(self):
        s = allocate_portfolio(
            self.ops,
            SizingConfig(bankroll=1000, per_market_cap_frac=0.5, max_deployed_frac=0.2),
        )
        # Only $200 may be deployed; m2 depth 250 is capped by remaining bankroll.
        self.assertLessEqual(s.total_deployed, 200.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()
