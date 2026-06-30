import unittest
from datetime import datetime, timedelta, timezone

from polymarket_arb.favorites import (
    FavoriteBet,
    favorite_to_dict,
    find_favorites,
)
from polymarket_arb.models import CompleteSet, Leg, Level

NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


def market(market_id, days_out, legs):
    """legs: list of (outcome, ask_price, ask_size)."""
    end = None if days_out is None else (NOW + timedelta(days=days_out)).isoformat()
    return CompleteSet(
        market_id=market_id, question=market_id,
        legs=[Leg(f"t-{o}", o, Level(p, s), None) for o, p, s in legs],
        end_date=end, url="https://polymarket.com/event/x",
    )


class TestFindFavorites(unittest.TestCase):
    def test_in_band_near_dated_qualifies(self):
        sets = [market("m1", 1, [("Yes", 0.88, 100), ("No", 0.13, 100)])]
        favs = find_favorites(sets, min_price=0.80, max_price=0.91, min_size=5,
                              max_days=2, now=NOW)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0].outcome, "Yes")
        self.assertAlmostEqual(favs[0].payout_multiple, 1 / 0.88, places=3)

    def test_too_pricey_excluded(self):
        sets = [market("m1", 1, [("Yes", 0.97, 100), ("No", 0.04, 100)])]
        self.assertEqual(find_favorites(sets, max_price=0.91, max_days=2, now=NOW), [])

    def test_too_cheap_excluded(self):
        # 0.50 pays 2x — a coin flip, not "$1 -> $1.1".
        sets = [market("m1", 1, [("Yes", 0.50, 100), ("No", 0.50, 100)])]
        self.assertEqual(find_favorites(sets, min_price=0.80, max_days=2, now=NOW), [])

    def test_far_dated_excluded(self):
        sets = [market("m1", 10, [("Yes", 0.88, 100), ("No", 0.13, 100)])]
        self.assertEqual(find_favorites(sets, max_days=2, now=NOW), [])

    def test_unknown_date_excluded_when_window_set(self):
        sets = [market("m1", None, [("Yes", 0.88, 100), ("No", 0.13, 100)])]
        self.assertEqual(find_favorites(sets, max_days=2, now=NOW), [])

    def test_thin_depth_excluded(self):
        sets = [market("m1", 1, [("Yes", 0.88, 2), ("No", 0.13, 100)])]
        self.assertEqual(find_favorites(sets, min_size=5, max_days=2, now=NOW), [])

    def test_sorted_soonest_then_payout(self):
        sets = [
            market("later", 2, [("Yes", 0.82, 100), ("No", 0.20, 100)]),
            market("soon-low", 1, [("Yes", 0.90, 100), ("No", 0.11, 100)]),
            market("soon-high", 1, [("Yes", 0.82, 100), ("No", 0.20, 100)]),
        ]
        favs = find_favorites(sets, min_price=0.80, max_price=0.91, min_size=5,
                              max_days=3, now=NOW)
        self.assertEqual([f.market_id for f in favs], ["soon-high", "soon-low", "later"])

    def test_to_dict_shape(self):
        f = FavoriteBet("m", "q", "Yes", 0.88, 1 / 0.88, 0.88, 100, "2026-06-30T00:00:00Z", 1.0, "u")
        d = favorite_to_dict(f)
        self.assertEqual(d["outcome"], "Yes")
        self.assertEqual(d["price"], 0.88)
        self.assertEqual(d["payout_multiple"], round(1 / 0.88, 3))


class TestDemoAndNotify(unittest.TestCase):
    def test_demo_loader_yields_only_qualifying(self):
        from polymarket_arb.demo import load_demo_favorites
        favs = load_demo_favorites(min_price=0.80, max_price=0.91, min_size=5, max_days=2)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0].market_id, "fav-rain")

    def test_build_payload_demo(self):
        from polymarket_arb.notify import build_favorites_payload
        payload = build_favorites_payload(demo=True)
        self.assertEqual(payload["meta"]["source"], "demo")
        self.assertEqual(len(payload["favorites"]), 1)

    def test_notification_renders_and_dedups(self):
        from polymarket_arb.notify import build_favorites_payload, compute_notification
        payload = build_favorites_payload(demo=True)
        text, seen = compute_notification(payload, [])
        self.assertIsNotNone(text)
        self.assertIn("유력후보", text)
        self.assertIn("무위험 아님", text)          # honesty label
        self.assertIn("빗나가면 전액 손실", text)
        self.assertIn("$1 →", text)                 # payout framing
        # dedup
        self.assertIsNone(compute_notification(payload, seen)[0])


class _SpyClient:
    """Captures the token ids passed to order_books (to assert book_limit)."""

    def __init__(self, markets):
        self._markets = markets
        self.priced_ids = None

    def active_markets(self, extra_params=None):
        return self._markets

    def order_books(self, token_ids):
        self.priced_ids = list(token_ids)
        return {}


def _mkt(i, hours, price=0.88):
    end = (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "question": f"m{i}",
        "endDate": end,
        "outcomePrices": f'["{price}", "{round(1 - price, 2)}"]',
        "clobTokenIds": f'["tok{i}a", "tok{i}b"]',
    }


class TestFavoritesSpeed(unittest.TestCase):
    def test_book_limit_prices_only_soonest(self):
        from polymarket_arb.favorites import build_favorites_live
        # 5 in-band markets at 5,4,3,2,1 h; book_limit=2 -> price the 2 soonest.
        markets = [_mkt(i, hours=h) for i, h in enumerate([5, 4, 3, 2, 1])]
        client = _SpyClient(markets)
        build_favorites_live(client, min_price=0.80, max_price=0.91, min_size=1,
                             max_days=1.0, book_limit=2, now=NOW)
        self.assertEqual(set(client.priced_ids),
                         {"tok4a", "tok4b", "tok3a", "tok3b"})  # 1h + 2h markets

    def test_order_books_chunks_and_merges(self):
        from unittest import mock

        from polymarket_arb.client import PolymarketClient
        client = PolymarketClient()
        sizes = []

        class Resp:
            def __init__(self, ids):
                self._ids = ids

            def raise_for_status(self):
                pass

            def json(self):
                return [{"asset_id": t} for t in self._ids]

        def fake_post(url, json=None, timeout=None):
            ids = [d["token_id"] for d in json]
            sizes.append(len(ids))
            return Resp(ids)

        with mock.patch.object(client.session, "post", side_effect=fake_post):
            books = client.order_books([f"t{i}" for i in range(120)], chunk_size=50)
        self.assertEqual(len(books), 120)               # all merged
        self.assertEqual(sorted(sizes), [20, 50, 50])   # chunked (no giant POST)


if __name__ == "__main__":
    unittest.main()
