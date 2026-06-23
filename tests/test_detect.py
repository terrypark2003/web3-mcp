import unittest
from datetime import datetime, timezone

from polymarket_arb.detect import FeeModel, detect, scan_sets
from polymarket_arb.demo import load_demo_sets
from polymarket_arb.models import (
    ARB_BUY_SET,
    ARB_MINT_SELL,
    CompleteSet,
    Leg,
    Level,
)

NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)


def binary(yes_ask, yes_bid, no_ask, no_bid, neg_risk=False, end="2026-07-02T00:00:00Z"):
    def lvl(v):
        return None if v is None else Level(price=v[0], size=v[1])

    return CompleteSet(
        market_id="m",
        question="q",
        neg_risk=neg_risk,
        end_date=end,
        legs=[
            Leg("yes", "Yes", lvl(yes_ask), lvl(yes_bid)),
            Leg("no", "No", lvl(no_ask), lvl(no_bid)),
        ],
    )


class TestBuySet(unittest.TestCase):
    def test_profitable_buy_set(self):
        cs = binary((0.62, 120), (0.60, 150), (0.36, 90), (0.34, 80))
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_BUY_SET]
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertAlmostEqual(op.cost_per_set, 0.98, places=6)
        self.assertAlmostEqual(op.edge_per_set, 0.02, places=6)
        self.assertAlmostEqual(op.max_sets, 90.0, places=6)
        self.assertAlmostEqual(op.capital_required, 88.2, places=6)
        self.assertAlmostEqual(op.total_edge, 1.8, places=6)
        self.assertIsNotNone(op.annualized_pct)
        self.assertGreater(op.annualized_pct, 0)

    def test_no_arb_when_sum_at_or_above_one(self):
        cs = binary((0.51, 100), (0.49, 120), (0.51, 100), (0.49, 120))
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_BUY_SET]
        self.assertEqual(ops, [])

    def test_edge_below_min_is_filtered(self):
        cs = binary((0.60, 100), (0.50, 100), (0.398, 100), (0.30, 100))  # cost 0.998
        ops = [o for o in detect(cs, FeeModel(min_edge_per_set=0.005), NOW)
               if o.kind == ARB_BUY_SET]
        self.assertEqual(ops, [])

    def test_missing_ask_disqualifies(self):
        cs = binary((0.40, 100), (0.39, 100), None, (0.30, 100))
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_BUY_SET]
        self.assertEqual(ops, [])

    def test_thin_depth_filtered(self):
        cs = binary((0.40, 0.5), (0.39, 100), (0.40, 100), (0.30, 100))  # cost 0.80
        ops = [o for o in detect(cs, FeeModel(min_size=1.0), NOW)
               if o.kind == ARB_BUY_SET]
        self.assertEqual(ops, [])

    def test_fee_can_erase_edge(self):
        cs = binary((0.60, 100), (0.50, 100), (0.39, 100), (0.30, 100))  # cost 0.99, edge 0.01
        no_fee = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_BUY_SET]
        self.assertEqual(len(no_fee), 1)
        # 1% fee on 0.99 notional ~= 0.0099 -> net edge ~0.0001 < min 0.005
        with_fee = [o for o in detect(cs, FeeModel(taker_fee_rate=0.01), NOW)
                    if o.kind == ARB_BUY_SET]
        self.assertEqual(with_fee, [])

    def test_negrisk_three_way_buy_set(self):
        def lvl(p, s):
            return Level(price=p, size=s)

        cs = CompleteSet(
            market_id="e",
            question="3-way",
            neg_risk=True,
            end_date="2026-11-04T00:00:00Z",
            legs=[
                Leg("a", "A", lvl(0.30, 500), lvl(0.28, 400)),
                Leg("b", "B", lvl(0.33, 450), lvl(0.31, 420)),
                Leg("c", "C", lvl(0.34, 380), lvl(0.32, 360)),
            ],
        )
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_BUY_SET]
        self.assertEqual(len(ops), 1)
        self.assertAlmostEqual(ops[0].edge_per_set, 0.03, places=6)
        self.assertAlmostEqual(ops[0].max_sets, 380.0, places=6)


class TestMintSell(unittest.TestCase):
    def test_profitable_mint_sell(self):
        cs = binary((0.58, 200), (0.55, 300), (0.50, 200), (0.50, 250))  # bids 1.05
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_MINT_SELL]
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertAlmostEqual(op.proceeds_per_set, 1.05, places=6)
        self.assertAlmostEqual(op.edge_per_set, 0.05, places=6)
        self.assertAlmostEqual(op.max_sets, 250.0, places=6)
        self.assertIsNone(op.annualized_pct)  # instant

    def test_mint_sell_disabled_for_negrisk(self):
        def lvl(p, s):
            return Level(price=p, size=s)

        cs = CompleteSet(
            market_id="e", question="q", neg_risk=True, end_date=None,
            legs=[
                Leg("a", "A", lvl(0.60, 100), lvl(0.60, 100)),
                Leg("b", "B", lvl(0.60, 100), lvl(0.60, 100)),
            ],
        )
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_MINT_SELL]
        self.assertEqual(ops, [])

    def test_no_mint_sell_when_bids_below_one(self):
        cs = binary((0.62, 120), (0.60, 150), (0.36, 90), (0.34, 80))  # bids 0.94
        ops = [o for o in detect(cs, FeeModel(), NOW) if o.kind == ARB_MINT_SELL]
        self.assertEqual(ops, [])


class TestScanAndDemo(unittest.TestCase):
    def test_scan_sorted_by_total_edge_desc(self):
        ops = scan_sets(load_demo_sets(), FeeModel(), NOW)
        self.assertTrue(ops)
        edges = [o.total_edge for o in ops]
        self.assertEqual(edges, sorted(edges, reverse=True))

    def test_demo_finds_expected_kinds(self):
        ops = scan_sets(load_demo_sets(), FeeModel(), NOW)
        kinds = {o.kind for o in ops}
        self.assertIn(ARB_BUY_SET, kinds)
        self.assertIn(ARB_MINT_SELL, kinds)
        # The "no-arb" market must not appear at all.
        self.assertNotIn("demo-no-arb", {o.market_id for o in ops})


if __name__ == "__main__":
    unittest.main()
