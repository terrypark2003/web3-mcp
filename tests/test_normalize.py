import unittest

from polymarket_arb.normalize import (
    _maybe_json_list,
    complete_set_from_market,
    indicative_set_cost,
    top_of_book,
)


class TestMaybeJsonList(unittest.TestCase):
    def test_passthrough_list(self):
        self.assertEqual(_maybe_json_list(["a", "b"]), ["a", "b"])

    def test_json_encoded_string(self):
        self.assertEqual(_maybe_json_list('["Yes", "No"]'), ["Yes", "No"])

    def test_bad_string(self):
        self.assertEqual(_maybe_json_list("not json"), [])

    def test_none(self):
        self.assertEqual(_maybe_json_list(None), [])


class TestTopOfBook(unittest.TestCase):
    def test_picks_lowest_ask_highest_bid(self):
        book = {
            "asks": [{"price": "0.62", "size": "10"}, {"price": "0.60", "size": "5"}],
            "bids": [{"price": "0.55", "size": "8"}, {"price": "0.58", "size": "3"}],
        }
        ask, bid = top_of_book(book)
        self.assertEqual((ask.price, ask.size), (0.60, 5))
        self.assertEqual((bid.price, bid.size), (0.58, 3))

    def test_ignores_zero_size(self):
        book = {"asks": [{"price": "0.40", "size": "0"}], "bids": []}
        ask, bid = top_of_book(book)
        self.assertIsNone(ask)
        self.assertIsNone(bid)

    def test_empty(self):
        self.assertEqual(top_of_book(None), (None, None))
        self.assertEqual(top_of_book({}), (None, None))


class TestCompleteSetFromMarket(unittest.TestCase):
    def test_builds_binary_set_with_books(self):
        market = {
            "id": "123",
            "question": "Will X happen?",
            "endDate": "2026-07-02T00:00:00Z",
            "negRisk": False,
            "clobTokenIds": '["tok-yes", "tok-no"]',
            "outcomes": '["Yes", "No"]',
        }
        books = {
            "tok-yes": {"asks": [{"price": "0.62", "size": "120"}],
                        "bids": [{"price": "0.60", "size": "150"}]},
            "tok-no": {"asks": [{"price": "0.36", "size": "90"}],
                       "bids": [{"price": "0.34", "size": "80"}]},
        }
        cs = complete_set_from_market(market, books)
        self.assertEqual(cs.market_id, "123")
        self.assertEqual(len(cs.legs), 2)
        self.assertEqual(cs.legs[0].outcome, "Yes")
        self.assertEqual(cs.legs[0].best_ask.price, 0.62)
        self.assertEqual(cs.legs[1].best_bid.price, 0.34)

    def test_returns_none_on_token_outcome_mismatch(self):
        market = {"id": "1", "clobTokenIds": '["only-one"]', "outcomes": '["Yes", "No"]'}
        self.assertIsNone(complete_set_from_market(market, {}))


class TestIndicativeCost(unittest.TestCase):
    def test_sums_outcome_prices(self):
        self.assertAlmostEqual(
            indicative_set_cost({"outcomePrices": '["0.62", "0.36"]'}), 0.98, places=6
        )

    def test_missing_prices(self):
        self.assertIsNone(indicative_set_cost({}))


if __name__ == "__main__":
    unittest.main()
