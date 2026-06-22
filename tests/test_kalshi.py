import unittest

from polymarket_arb.kalshi_normalize import (
    complete_set_from_kalshi,
    indicative_kalshi_cost,
)
from polymarket_arb.matching import Pair, build_matched_markets
from polymarket_arb.models import CompleteSet, Leg, Level
from polymarket_arb.venues import KALSHI, POLYMARKET


class TestKalshiNormalize(unittest.TestCase):
    def test_orderbook_to_yes_no_legs(self):
        market = {
            "ticker": "KXBTC-26-100K",
            "title": "BTC > 100k?",
            "close_time": "2026-12-31T23:59:59Z",
        }
        # yes bid 53c (size 800), no bid 44c (size 900).
        book = {"orderbook": {"yes": [[53, 800]], "no": [[44, 900]]}}
        cs = complete_set_from_kalshi(market, book)
        self.assertEqual(cs.venue, KALSHI)
        self.assertEqual(cs.market_id, "KXBTC-26-100K")
        yes, no = cs.legs
        # yes_ask = 1 - best_no_bid = 1 - 0.44 = 0.56 (size from no side = 900).
        self.assertAlmostEqual(yes.best_ask.price, 0.56)
        self.assertEqual(yes.best_ask.size, 900)
        # yes_bid = best_yes_bid = 0.53.
        self.assertAlmostEqual(yes.best_bid.price, 0.53)
        # no_ask = 1 - best_yes_bid = 1 - 0.53 = 0.47 (size from yes side = 800).
        self.assertAlmostEqual(no.best_ask.price, 0.47)
        self.assertEqual(no.best_ask.size, 800)
        self.assertAlmostEqual(no.best_bid.price, 0.44)

    def test_picks_best_bid_from_multiple_levels(self):
        market = {"ticker": "T", "title": "t"}
        book = {"orderbook": {"yes": [[40, 10], [53, 5], [50, 20]], "no": [[44, 1]]}}
        cs = complete_set_from_kalshi(market, book)
        yes, _ = cs.legs
        self.assertAlmostEqual(yes.best_bid.price, 0.53)  # highest yes bid

    def test_missing_side_yields_none_quotes(self):
        market = {"ticker": "T", "title": "t"}
        book = {"orderbook": {"yes": [[53, 800]], "no": []}}
        cs = complete_set_from_kalshi(market, book)
        yes, no = cs.legs
        self.assertIsNone(yes.best_ask)   # needs a no bid to price a yes ask
        self.assertIsNotNone(yes.best_bid)
        self.assertIsNotNone(no.best_ask)

    def test_no_ticker_returns_none(self):
        self.assertIsNone(complete_set_from_kalshi({"title": "x"}, None))

    def test_indicative_cost(self):
        self.assertAlmostEqual(
            indicative_kalshi_cost({"yes_ask": 56, "no_ask": 47}), 1.03
        )
        self.assertIsNone(indicative_kalshi_cost({"yes_ask": 56}))


class TestMatching(unittest.TestCase):
    def _poly(self, mid):
        return CompleteSet(mid, "q", venue=POLYMARKET, legs=[
            Leg("y", "Yes", Level(0.49, 100), None, venue=POLYMARKET),
            Leg("n", "No", Level(0.52, 100), None, venue=POLYMARKET),
        ])

    def _kalshi(self, ticker):
        return CompleteSet(ticker, "q", venue=KALSHI, legs=[
            Leg("y", "Yes", Level(0.56, 100), None, venue=KALSHI),
            Leg("n", "No", Level(0.47, 100), None, venue=KALSHI),
        ])

    def test_builds_matched_when_both_present(self):
        pairs = [Pair("e", "q", "K", "P", end_date="2026-12-31T00:00:00Z")]
        matched = build_matched_markets(
            pairs, {"K": self._kalshi("K")}, {"P": self._poly("P")}
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].venue_a.venue, KALSHI)
        self.assertEqual(matched[0].venue_b.venue, POLYMARKET)

    def test_skips_when_leg_missing(self):
        pairs = [Pair("e", "q", "K", "P")]
        self.assertEqual(
            build_matched_markets(pairs, {}, {"P": self._poly("P")}), []
        )


if __name__ == "__main__":
    unittest.main()
