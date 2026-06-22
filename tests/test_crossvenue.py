import math
import unittest

from polymarket_arb.crossvenue import (
    ARB_CROSS_VENUE,
    MatchedMarket,
    detect_cross_venue,
    scan_cross_venue,
)
from polymarket_arb.models import CompleteSet, Leg, Level
from polymarket_arb.venues import KALSHI, POLYMARKET, KalshiFee, default_venue_fees


def binary_set(market_id, venue, yes_ask, no_ask, size=1000, yes_bid=None, no_bid=None):
    return CompleteSet(
        market_id=market_id,
        question=f"q {market_id}",
        venue=venue,
        legs=[
            Leg("y", "Yes", Level(yes_ask, size),
                Level(yes_bid, size) if yes_bid else None, venue=venue),
            Leg("n", "No", Level(no_ask, size),
                Level(no_bid, size) if no_bid else None, venue=venue),
        ],
    )


class TestKalshiFee(unittest.TestCase):
    def test_formula_rounds_up_to_cent(self):
        fee = KalshiFee(name=KALSHI)
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounds up to 0.02
        self.assertEqual(fee.fee(0.50, 1), 0.02)

    def test_fee_zero_at_tails(self):
        fee = KalshiFee(name=KALSHI)
        self.assertEqual(fee.fee(0.0, 100), 0.0)
        self.assertEqual(fee.fee(1.0, 100), 0.0)

    def test_scales_with_contracts(self):
        fee = KalshiFee(name=KALSHI)
        self.assertAlmostEqual(fee.fee(0.47, 800), 13.95, places=2)


class TestCrossVenue(unittest.TestCase):
    def test_detects_cheaper_cross_pairing(self):
        # YES cheaper on poly (0.49), NO cheaper on kalshi (0.47) -> 0.96 gross.
        kalshi = binary_set("K", KALSHI, yes_ask=0.56, no_ask=0.47, size=800)
        poly = binary_set("P", POLYMARKET, yes_ask=0.49, no_ask=0.52, size=1000)
        m = MatchedMarket("evt", "BTC?", kalshi, poly, end_date=None)
        op = detect_cross_venue(m, default_venue_fees())
        self.assertIsNotNone(op)
        self.assertEqual(op.kind, ARB_CROSS_VENUE)
        self.assertEqual(op.yes_venue, POLYMARKET)
        self.assertEqual(op.no_venue, KALSHI)
        self.assertEqual(op.max_sets, 800)  # depth-limited to thinner leg
        # gross edge 0.04/set, minus ~0.0174/set Kalshi fee.
        self.assertAlmostEqual(op.edge_per_set, 0.04 - 13.95 / 800, places=4)

    def test_fee_erases_thin_gap(self):
        # Raw 1.5c gap; Kalshi fee on the NO leg wipes it out.
        kalshi = binary_set("K", KALSHI, yes_ask=0.62, no_ask=0.40, size=500)
        poly = binary_set("P", POLYMARKET, yes_ask=0.585, no_ask=0.42, size=500)
        m = MatchedMarket("evt", "Fed?", kalshi, poly)
        self.assertIsNone(detect_cross_venue(m, default_venue_fees()))

    def test_no_arb_when_both_sides_expensive(self):
        kalshi = binary_set("K", KALSHI, yes_ask=0.60, no_ask=0.60, size=500)
        poly = binary_set("P", POLYMARKET, yes_ask=0.60, no_ask=0.60, size=500)
        m = MatchedMarket("evt", "q", kalshi, poly)
        self.assertIsNone(detect_cross_venue(m, default_venue_fees()))

    def test_missing_ask_returns_none(self):
        kalshi = CompleteSet("K", "q", venue=KALSHI, legs=[
            Leg("y", "Yes", None, None, venue=KALSHI),
            Leg("n", "No", Level(0.40, 100), None, venue=KALSHI),
        ])
        poly = binary_set("P", POLYMARKET, 0.49, 0.52)
        m = MatchedMarket("evt", "q", kalshi, poly)
        # YES@kalshi unpriced; YES@poly(0.49)+NO@kalshi(0.40)=0.89 still works.
        op = detect_cross_venue(m, default_venue_fees())
        self.assertIsNotNone(op)
        self.assertEqual(op.yes_venue, POLYMARKET)

    def test_apr_for_dated_event(self):
        kalshi = binary_set("K", KALSHI, 0.56, 0.47, size=800)
        poly = binary_set("P", POLYMARKET, 0.49, 0.52, size=1000)
        m = MatchedMarket("evt", "q", kalshi, poly, end_date="2125-01-01T00:00:00Z")
        op = detect_cross_venue(m, default_venue_fees())
        self.assertIsNotNone(op.annualized_pct)
        self.assertGreater(op.annualized_pct, 0)

    def test_scan_sorts_by_total_edge(self):
        big = MatchedMarket("big", "q",
                            binary_set("K1", KALSHI, 0.56, 0.30, size=800),
                            binary_set("P1", POLYMARKET, 0.49, 0.52, size=1000))
        small = MatchedMarket("small", "q",
                              binary_set("K2", KALSHI, 0.56, 0.47, size=800),
                              binary_set("P2", POLYMARKET, 0.49, 0.52, size=1000))
        ops = scan_cross_venue([small, big], default_venue_fees())
        self.assertEqual([o.event_id for o in ops], ["big", "small"])


if __name__ == "__main__":
    unittest.main()
