import unittest

from polymarket_arb.normalize import (
    _maybe_json_list,
    complete_set_from_event,
    complete_set_from_market,
    indicative_event_cost,
    indicative_set_cost,
    market_url,
    submarket_yes_token,
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


def _submarket(group_title, yes_token, no_token, yes_price, no_price):
    return {
        "groupItemTitle": group_title,
        "question": f"Will {group_title} win?",
        "clobTokenIds": f'["{yes_token}", "{no_token}"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes_price}", "{no_price}"]',
    }


def _event(neg_risk=True):
    return {
        "id": "evt-1",
        "title": "Who wins?",
        "endDate": "2026-11-04T00:00:00Z",
        "negRisk": neg_risk,
        "markets": [
            _submarket("A", "a-yes", "a-no", "0.30", "0.70"),
            _submarket("B", "b-yes", "b-no", "0.33", "0.67"),
            _submarket("C", "c-yes", "c-no", "0.34", "0.66"),
        ],
    }


class TestSubmarketYesToken(unittest.TestCase):
    def test_picks_yes_token_by_outcome(self):
        m = _submarket("A", "a-yes", "a-no", "0.3", "0.7")
        self.assertEqual(submarket_yes_token(m), "a-yes")

    def test_falls_back_to_first_token(self):
        m = {"clobTokenIds": '["only"]', "outcomes": "[]"}
        self.assertEqual(submarket_yes_token(m), "only")


class TestCompleteSetFromEvent(unittest.TestCase):
    def test_builds_three_yes_legs_with_books(self):
        books = {
            "a-yes": {"asks": [{"price": "0.30", "size": "500"}], "bids": []},
            "b-yes": {"asks": [{"price": "0.33", "size": "450"}], "bids": []},
            "c-yes": {"asks": [{"price": "0.34", "size": "380"}], "bids": []},
        }
        cs = complete_set_from_event(_event(), books)
        self.assertEqual(len(cs.legs), 3)
        self.assertTrue(cs.neg_risk)
        self.assertTrue(cs.exhaustive)
        self.assertEqual([leg.outcome for leg in cs.legs], ["A", "B", "C"])
        self.assertEqual(cs.legs[0].best_ask.price, 0.30)

    def test_non_negrisk_event_not_exhaustive(self):
        cs = complete_set_from_event(_event(neg_risk=False), {})
        self.assertFalse(cs.neg_risk)
        self.assertFalse(cs.exhaustive)

    def test_returns_none_with_too_few_legs(self):
        event = {"id": "e", "title": "t", "markets": [
            _submarket("A", "a-yes", "a-no", "0.5", "0.5")]}
        self.assertIsNone(complete_set_from_event(event, {}))

    def test_indicative_event_cost_sums_yes_prices(self):
        self.assertAlmostEqual(indicative_event_cost(_event()), 0.97, places=6)


class TestMarketUrl(unittest.TestCase):
    def test_builds_from_own_slug(self):
        self.assertEqual(
            market_url({"slug": "openai-ipo-closing-market-cap"}),
            "https://polymarket.com/event/openai-ipo-closing-market-cap",
        )

    def test_prefers_parent_event_slug(self):
        obj = {"slug": "will-argentina-win", "events": [{"slug": "world-cup-2026-winner"}]}
        self.assertEqual(
            market_url(obj), "https://polymarket.com/event/world-cup-2026-winner"
        )

    def test_none_without_slug(self):
        self.assertIsNone(market_url({"id": "123"}))

    def test_complete_set_from_market_carries_url(self):
        market = {
            "id": "1", "slug": "will-x-happen",
            "clobTokenIds": '["t-yes", "t-no"]', "outcomes": '["Yes", "No"]',
        }
        cs = complete_set_from_market(market, {})
        self.assertEqual(cs.url, "https://polymarket.com/event/will-x-happen")

    def test_complete_set_from_event_carries_url(self):
        event = dict(_event())
        event["slug"] = "who-wins"
        cs = complete_set_from_event(event, {})
        self.assertEqual(cs.url, "https://polymarket.com/event/who-wins")


if __name__ == "__main__":
    unittest.main()
