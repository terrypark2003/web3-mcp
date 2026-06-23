"""Offline integration tests for the live scan path, using a fake client.

Egress blocks real Polymarket calls in this environment, so we stub the HTTP
client and verify that build_sets_live / scan_live correctly assemble binary
markets *and* negative-risk event groups and run them through detection.
"""

import unittest

from polymarket_arb.detect import FeeModel
from polymarket_arb.models import ARB_BUY_SET
from polymarket_arb.scanner import scan_live


class FakeClient:
    def __init__(self, markets, events, books):
        self._markets = markets
        self._events = events
        self._books = books

    def active_markets(self):
        return self._markets

    def active_events(self):
        return self._events

    def order_books(self, token_ids):
        return {t: self._books[t] for t in token_ids if t in self._books}


def ask_book(price, size):
    return {"asks": [{"price": str(price), "size": str(size)}], "bids": []}


def submarket(title, yes_token, no_token, yes_price, no_price):
    return {
        "groupItemTitle": title,
        "clobTokenIds": f'["{yes_token}", "{no_token}"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes_price}", "{no_price}"]',
    }


class TestScanLive(unittest.TestCase):
    def setUp(self):
        self.markets = [
            {
                "id": "m1",
                "question": "Will it rain?",
                "endDate": "2026-07-02T00:00:00Z",
                "negRisk": False,
                "clobTokenIds": '["yes1", "no1"]',
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.62", "0.36"]',
            }
        ]
        self.events = [
            {
                "id": "evt1",
                "title": "Who wins?",
                "endDate": "2026-11-04T00:00:00Z",
                "negRisk": True,
                "markets": [
                    submarket("A", "a-yes", "a-no", "0.30", "0.70"),
                    submarket("B", "b-yes", "b-no", "0.33", "0.67"),
                    submarket("C", "c-yes", "c-no", "0.34", "0.66"),
                ],
            }
        ]
        self.books = {
            "yes1": ask_book(0.62, 120),
            "no1": ask_book(0.36, 90),
            "a-yes": ask_book(0.30, 500),
            "b-yes": ask_book(0.33, 450),
            "c-yes": ask_book(0.34, 380),
        }

    def test_finds_binary_and_event_arbs(self):
        client = FakeClient(self.markets, self.events, self.books)
        ops = scan_live(client, FeeModel())
        by_market = {o.market_id: o for o in ops}

        self.assertIn("m1", by_market)
        self.assertEqual(by_market["m1"].kind, ARB_BUY_SET)
        self.assertAlmostEqual(by_market["m1"].edge_per_set, 0.02, places=6)
        self.assertEqual(by_market["m1"].n_legs, 2)

        self.assertIn("evt1", by_market)
        evt = by_market["evt1"]
        self.assertEqual(evt.kind, ARB_BUY_SET)
        self.assertEqual(evt.n_legs, 3)
        self.assertTrue(evt.neg_risk)
        self.assertAlmostEqual(evt.edge_per_set, 0.03, places=6)
        # buy-all-Yes depth is the thinnest leg (c-yes, 380 shares)
        self.assertAlmostEqual(evt.max_sets, 380.0, places=6)

    def test_non_negrisk_event_is_skipped(self):
        non_neg = dict(self.events[0], negRisk=False)
        client = FakeClient([], [non_neg], self.books)
        ops = scan_live(client, FeeModel())
        self.assertEqual(ops, [])


if __name__ == "__main__":
    unittest.main()
