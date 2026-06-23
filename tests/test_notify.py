import unittest

from polymarket_arb.notify import compute_notification

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
        self.assertIn("Polymarket:", text)
        self.assertIn("Cross-venue:", text)
        self.assertIn("(live)", text)
        # EV excluded by default
        self.assertNotIn("Positive-EV", text)

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
        self.assertIn("Positive-EV", text)
        self.assertIn("NOT risk-free", text)

    def test_seen_keys_are_json_serializable(self):
        import json
        _, seen = compute_notification(PAYLOAD, [])
        json.loads(json.dumps(seen))  # must round-trip for the state file


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
        self.assertIn("World Cup value", text)
        self.assertIn("Argentina", text)
        self.assertIn("consensus", text)
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
