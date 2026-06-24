import unittest
from datetime import datetime, timezone

from polymarket_arb.notify import _resolution_eta, compute_notification

PAYLOAD = {
    "polymarket": [
        {"market_id": "p1", "kind": "BUY_SET", "question": "Rain?", "edge_pct": 2.0,
         "total_edge": 5, "annualized_pct": 50.0},
    ],
    "cross_venue": [
        {"event_id": "btc", "yes_venue": "polymarket", "no_venue": "kalshi",
         "question": "BTC?", "edge_pct": 2.35, "total_edge": 18,
         "yes_price": 0.49, "no_price": 0.47},
    ],
    "ev": [
        {"market_id": "e1", "side": "YES", "venue": "polymarket", "question": "EV?",
         "edge_pct": 26.0, "price": 0.49, "fair_prob": 0.62, "ev_per_contract": 0.13},
    ],
    "meta": {"source": "live"},
}


class TestComputeNotification(unittest.TestCase):
    def test_first_run_fires_for_risk_free(self):
        text, seen = compute_notification(PAYLOAD, [])
        self.assertIsNotNone(text)
        self.assertIn("폴리마켓:", text)
        self.assertIn("크로스 거래소:", text)
        self.assertIn("(실시간)", text)
        # EV excluded by default
        self.assertNotIn("포지티브 EV", text)

    def test_dedups_second_run(self):
        _, seen = compute_notification(PAYLOAD, [])
        text2, _ = compute_notification(PAYLOAD, seen)
        self.assertIsNone(text2)

    def test_new_edge_fires_after_first(self):
        _, seen = compute_notification(PAYLOAD, [])
        payload2 = {
            "polymarket": PAYLOAD["polymarket"] + [
                {"market_id": "p2", "kind": "MINT_SELL", "question": "Fed?",
                 "edge_pct": 5.0, "total_edge": 12, "annualized_pct": None},
            ],
            "cross_venue": PAYLOAD["cross_venue"],
            "ev": [],
            "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload2, seen)
        self.assertIsNotNone(text)
        self.assertIn("Fed?", text)     # the new edge fires
        self.assertNotIn("Rain?", text)  # p1 already seen, not repeated

    def test_vanished_then_reappear_refires(self):
        _, seen = compute_notification(PAYLOAD, [])
        empty = {"polymarket": [], "cross_venue": [], "ev": [], "meta": {"source": "live"}}
        text_gone, seen2 = compute_notification(empty, seen)
        self.assertIsNone(text_gone)
        text_back, _ = compute_notification(PAYLOAD, seen2)
        self.assertIsNotNone(text_back)

    def test_min_edge_filters(self):
        text, _ = compute_notification(PAYLOAD, [], min_edge_pct=3.0)
        # Only cross (2.35) and poly (2.0) exist, both below 3.0 -> nothing.
        self.assertIsNone(text)

    def test_include_ev_flag(self):
        text, _ = compute_notification(PAYLOAD, [], include_ev=True)
        self.assertIn("포지티브 EV", text)
        self.assertIn("무위험 아님", text)

    def test_seen_keys_are_json_serializable(self):
        import json
        _, seen = compute_notification(PAYLOAD, [])
        json.loads(json.dumps(seen))  # must round-trip for the state file


class TestActionableRendering(unittest.TestCase):
    def test_poly_small_edge_shows_cents_not_zero(self):
        # A $0.32 profit must not round to "$0" (the old bug that read as "nothing").
        payload = {
            "polymarket": [
                {"market_id": "p9", "kind": "BUY_SET", "question": "OpenAI IPO?",
                 "edge_pct": 1.52, "total_edge": 0.32, "capital_required": 21.0,
                 "annualized_pct": 3.0,
                 "url": "https://polymarket.com/event/openai-ipo"},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("$0.32", text)        # real profit, not rounded to "$0"
        self.assertIn("보장수익", text)       # plain-language term, not bare "edge"
        # An explicit action, not bare jargon.
        self.assertIn("정산 시 $1 회수", text)
        # A tappable market link.
        self.assertIn("https://polymarket.com/event/openai-ipo", text)
        # The glossary explains what the term means.
        self.assertIn("보장수익(edge)", text)

    def test_link_omitted_when_url_absent(self):
        text, _ = compute_notification(PAYLOAD, [])  # PAYLOAD has no url
        self.assertNotIn("\U0001f517", text)  # no link emoji / dangling link

    def test_poly_buy_set_shows_resolution_eta(self):
        payload = {
            "polymarket": [
                {"market_id": "p1", "kind": "BUY_SET", "question": "Rain?",
                 "edge_pct": 2.0, "total_edge": 5, "annualized_pct": 50.0,
                 "end_date": "2099-01-01T00:00:00Z"},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("정산까지 약", text)

    def test_mint_sell_omits_eta(self):
        # MINT_SELL settles instantly — no holding period to show.
        payload = {
            "polymarket": [
                {"market_id": "p2", "kind": "MINT_SELL", "question": "Fed?",
                 "edge_pct": 5.0, "total_edge": 12, "annualized_pct": None,
                 "end_date": "2099-01-01T00:00:00Z"},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertNotIn("정산까지 약", text)  # the ETA marker (glossary wording differs)

    def test_world_cup_shows_buy_side_and_link(self):
        payload = {
            "polymarket": [], "cross_venue": [], "ev": [],
            "world_cup": [
                {"market_id": "wc-arg", "side": "NO", "venue": "polymarket",
                 "question": "Will Argentina win the 2026 World Cup?", "price": 0.80,
                 "fair_prob": 0.83, "ev_per_contract": 0.03, "edge_pct": 3.8,
                 "url": "https://polymarket.com/event/world-cup-2026"},
            ],
            "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("NO 0.80에 매수", text)
        self.assertIn("https://polymarket.com/event/world-cup-2026", text)
        self.assertIn("기대우위(edge)", text)  # glossary for the EV-style term


class TestResolutionEta(unittest.TestCase):
    NOW = datetime(2026, 6, 24, tzinfo=timezone.utc)

    def test_days(self):
        self.assertEqual(
            _resolution_eta("2026-07-15T00:00:00Z", self.NOW), "정산까지 약 21일"
        )

    def test_months(self):
        self.assertEqual(
            _resolution_eta("2026-12-24T00:00:00Z", self.NOW), "정산까지 약 6개월"
        )

    def test_hours(self):
        self.assertEqual(
            _resolution_eta("2026-06-24T05:00:00Z", self.NOW), "정산까지 약 5시간"
        )

    def test_past_is_flagged(self):
        self.assertEqual(
            _resolution_eta("2026-06-01T00:00:00Z", self.NOW), "정산 시점 지남"
        )

    def test_missing_or_bad_is_none(self):
        self.assertIsNone(_resolution_eta(None, self.NOW))
        self.assertIsNone(_resolution_eta("not-a-date", self.NOW))


WC_PAYLOAD = {
    "polymarket": [], "cross_venue": [], "ev": [],
    "world_cup": [
        {"market_id": "wc-arg", "side": "NO", "venue": "polymarket",
         "question": "Will Argentina win the 2026 World Cup?", "price": 0.80,
         "fair_prob": 0.83, "ev_per_contract": 0.03, "edge_pct": 3.8},
    ],
    "meta": {"source": "live"},
}


class TestWorldCupNotification(unittest.TestCase):
    def test_world_cup_section_renders_and_dedups(self):
        text, seen = compute_notification(WC_PAYLOAD, [])
        self.assertIsNotNone(text)
        self.assertIn("월드컵 가치", text)
        self.assertIn("Argentina", text)
        self.assertIn("컨센서스", text)
        self.assertIsNone(compute_notification(WC_PAYLOAD, seen)[0])  # dedup

    def test_demo_payload_builds(self):
        from polymarket_arb.notify import build_world_cup_payload
        payload = build_world_cup_payload(demo=True)
        self.assertEqual(payload["meta"]["source"], "demo")
        self.assertTrue(payload["world_cup"])  # demo fixture yields value bets

    def test_live_without_key_is_error_not_crash(self):
        import os
        from polymarket_arb.notify import build_world_cup_payload
        old = os.environ.pop("ODDS_API_KEY", None)
        try:
            payload = build_world_cup_payload(demo=False)
        finally:
            if old is not None:
                os.environ["ODDS_API_KEY"] = old
        self.assertEqual(payload["meta"]["source"], "error")
        self.assertIn("ODDS_API_KEY", payload["meta"]["error"])


if __name__ == "__main__":
    unittest.main()
