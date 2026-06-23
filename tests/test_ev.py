import unittest

from polymarket_arb.ev import (
    EV_SIGNAL,
    detect_ev,
    fair_value_from_map,
    scan_ev,
)
from polymarket_arb.models import CompleteSet, Leg, Level
from polymarket_arb.venues import KALSHI, POLYMARKET, default_venue_fees


def binary_set(market_id, venue, yes_ask, no_ask, size=1000):
    return CompleteSet(
        market_id=market_id,
        question=f"q {market_id}",
        venue=venue,
        legs=[
            Leg("y", "Yes", Level(yes_ask, size), None, venue=venue),
            Leg("n", "No", Level(no_ask, size), None, venue=venue),
        ],
    )


class TestEV(unittest.TestCase):
    def test_flags_underpriced_yes(self):
        cs = binary_set("m", POLYMARKET, yes_ask=0.49, no_ask=0.52)
        fair = fair_value_from_map({"m": 0.62})
        ops = detect_ev(cs, fair, default_venue_fees(), min_ev=0.02)
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertEqual(op.kind, EV_SIGNAL)
        self.assertEqual(op.side, "YES")
        self.assertAlmostEqual(op.ev_per_contract, 0.62 - 0.49, places=6)

    def test_flags_underpriced_no(self):
        # fair P(YES)=0.45 -> P(NO)=0.55; NO ask 0.42 -> EV 0.13.
        cs = binary_set("m", POLYMARKET, yes_ask=0.60, no_ask=0.42)
        fair = fair_value_from_map({"m": 0.45})
        ops = detect_ev(cs, fair, default_venue_fees(), min_ev=0.02)
        self.assertEqual([o.side for o in ops], ["NO"])
        self.assertAlmostEqual(ops[0].ev_per_contract, 0.55 - 0.42, places=6)

    def test_kalshi_fee_reduces_ev(self):
        cs = binary_set("m", KALSHI, yes_ask=0.56, no_ask=0.46)
        fair = fair_value_from_map({"m": 0.62})
        ops = detect_ev(cs, fair, default_venue_fees(), min_ev=0.0)
        yes = next(o for o in ops if o.side == "YES")
        # EV = 0.62 - 0.56 - kalshi_fee(0.56,1)=0.02 -> 0.04
        self.assertAlmostEqual(yes.ev_per_contract, 0.04, places=6)

    def test_no_fair_value_yields_nothing(self):
        cs = binary_set("m", POLYMARKET, 0.49, 0.52)
        ops = detect_ev(cs, fair_value_from_map({}), default_venue_fees())
        self.assertEqual(ops, [])

    def test_threshold_filters(self):
        cs = binary_set("m", POLYMARKET, yes_ask=0.60, no_ask=0.41)
        fair = fair_value_from_map({"m": 0.61})  # YES EV only 0.01
        self.assertEqual(detect_ev(cs, fair, min_ev=0.05), [])

    def test_out_of_range_prob_ignored(self):
        cs = binary_set("m", POLYMARKET, 0.49, 0.52)
        self.assertEqual(detect_ev(cs, fair_value_from_map({"m": 1.5})), [])

    def test_scan_sorts_by_ev(self):
        a = binary_set("a", POLYMARKET, 0.49, 0.52)  # EV 0.13
        b = binary_set("b", POLYMARKET, 0.55, 0.46)  # EV 0.07
        fair = fair_value_from_map({"a": 0.62, "b": 0.62})
        ops = scan_ev([b, a], fair, default_venue_fees(), min_ev=0.02)
        self.assertEqual([o.market_id for o in ops], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
