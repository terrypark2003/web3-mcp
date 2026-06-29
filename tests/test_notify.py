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


class TestRealismRendering(unittest.TestCase):
    def test_confidence_line_shown_when_present(self):
        payload = {
            "polymarket": [
                {"market_id": "p1", "kind": "BUY_SET", "question": "Rain?",
                 "edge_pct": 2.0, "total_edge": 5, "annualized_pct": 50.0,
                 "confidence": 72, "executable_sets": 120, "net_total_edge": 2.4,
                 "feasible_min_order": True},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("현실성 72/100", text)
        self.assertIn("🟢", text)               # high-confidence marker
        self.assertIn("실행가능 120세트", text)

    def test_low_confidence_red_marker(self):
        payload = {
            "polymarket": [
                {"market_id": "p1", "kind": "BUY_SET", "question": "Thin?",
                 "edge_pct": 4.0, "total_edge": 0.2, "annualized_pct": 8.0,
                 "confidence": 9, "executable_sets": 0, "net_total_edge": 0.0,
                 "feasible_min_order": False},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("🔴", text)
        self.assertIn("실행 어려움", text)

    def test_min_confidence_filters(self):
        payload = {
            "polymarket": [
                {"market_id": "p1", "kind": "BUY_SET", "question": "Thin?",
                 "edge_pct": 4.0, "total_edge": 0.2, "annualized_pct": 8.0,
                 "confidence": 9, "feasible_min_order": False},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [], min_confidence=30.0)
        self.assertIsNone(text)  # below the realism bar -> not sent

    def test_world_cup_value_line_speaks_dollars(self):
        # price 0.80, fair 0.83 -> $1 stake is worth ~0.83/0.80 = $1.04 at fair odds.
        text, _ = compute_notification(WC_PAYLOAD, [])
        self.assertIn("$1 → 약 $1.04 가치", text)


class TestResolutionWindow(unittest.TestCase):
    def _payload(self, kind, end_date):
        return {
            "polymarket": [
                {"market_id": "p1", "kind": kind, "question": "Q?", "edge_pct": 2.0,
                 "total_edge": 5, "annualized_pct": None if kind == "MINT_SELL" else 50.0,
                 "end_date": end_date},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }

    def test_near_dated_buy_set_passes(self):
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        text, _ = compute_notification(self._payload("BUY_SET", soon), [], max_days_to_resolution=2)
        self.assertIsNotNone(text)

    def test_far_dated_buy_set_filtered_out(self):
        from datetime import datetime, timedelta, timezone
        far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        text, _ = compute_notification(self._payload("BUY_SET", far), [], max_days_to_resolution=2)
        self.assertIsNone(text)  # resolves in 30 days -> dropped

    def test_mint_sell_kept_even_if_far_dated(self):
        from datetime import datetime, timedelta, timezone
        far = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        text, _ = compute_notification(self._payload("MINT_SELL", far), [], max_days_to_resolution=2)
        self.assertIsNotNone(text)  # instant settle -> window doesn't apply

    def test_unknown_date_dropped_when_window_active(self):
        text, _ = compute_notification(self._payload("BUY_SET", None), [], max_days_to_resolution=2)
        self.assertIsNone(text)  # can't promise "within N days" without a date

    def test_no_window_keeps_everything(self):
        from datetime import datetime, timedelta, timezone
        far = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        text, _ = compute_notification(self._payload("BUY_SET", far), [])  # max_days=None
        self.assertIsNotNone(text)


class TestResolutionEta(unittest.TestCase):
    NOW = datetime(2026, 6, 24, tzinfo=timezone.utc)

    def test_days(self):
        self.assertEqual(
            _resolution_eta("2026-07-15T00:00:00Z", self.NOW), "정산까지 약 21일"
        )

    def test_long_horizon_in_days(self):
        self.assertEqual(
            _resolution_eta("2026-12-24T00:00:00Z", self.NOW), "정산까지 약 183일"
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


class TestBuyList(unittest.TestCase):
    def test_negrisk_buy_set_lists_each_yes_with_cents(self):
        payload = {
            "polymarket": [
                {"market_id": "neg1", "kind": "BUY_SET", "question": "OpenAI IPO cap?",
                 "edge_pct": 3.95, "total_edge": 0.19, "capital_required": 4.81,
                 "annualized_pct": 8.0, "neg_risk": True,
                 "legs": [
                     {"outcome": "<500B", "ask": 0.017},
                     {"outcome": "No IPO by December 31, 2026", "ask": 0.48},
                 ]},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("이 결과들을 같은 수량으로 매수", text)
        self.assertIn("<500B → Yes", text)
        self.assertIn("1.7¢", text)
        self.assertIn("No IPO by December 31, 2026 → Yes", text)
        self.assertIn("48.0¢", text)

    def test_binary_buy_set_keeps_literal_yes_no(self):
        payload = {
            "polymarket": [
                {"market_id": "b1", "kind": "BUY_SET", "question": "Rain?",
                 "edge_pct": 2.0, "total_edge": 5, "annualized_pct": 50.0,
                 "legs": [
                     {"outcome": "Yes", "ask": 0.62},
                     {"outcome": "No", "ask": 0.36},
                 ]},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("• Yes  62.0¢", text)
        self.assertIn("• No  36.0¢", text)
        self.assertNotIn("Yes → Yes", text)  # don't double-label binary legs

    def test_mint_sell_has_no_buy_list(self):
        payload = {
            "polymarket": [
                {"market_id": "m1", "kind": "MINT_SELL", "question": "Fed?",
                 "edge_pct": 5.0, "total_edge": 12, "annualized_pct": None,
                 "legs": [{"outcome": "Yes", "ask": 0.5}, {"outcome": "No", "ask": 0.5}]},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertNotIn("같은 수량으로 매수", text)


class TestMinBuyin(unittest.TestCase):
    def test_cheap_leg_with_thin_depth_warns_infeasible(self):
        # OpenAI-IPO shape: cheapest leg 1.7¢ → 59 shares to clear $1, but only
        # ~5 sets of depth → the $1 floor breaks the arb.
        payload = {
            "polymarket": [
                {"market_id": "neg1", "kind": "BUY_SET", "question": "OpenAI IPO cap?",
                 "edge_pct": 3.95, "total_edge": 0.19, "capital_required": 4.81,
                 "cost_per_set": 0.962, "max_sets": 5, "edge_per_set": 0.038,
                 "annualized_pct": 8.0,
                 "legs": [{"outcome": "<500B", "ask": 0.017},
                          {"outcome": "No IPO", "ask": 0.48}]},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("최소 매수: 각 59주", text)   # ceil(1/0.017)
        self.assertIn("$56.76", text)              # 59 × 0.962
        self.assertIn("차익 소멸", text)            # infeasible vs depth

    def test_feasible_when_depth_covers_min(self):
        payload = {
            "polymarket": [
                {"market_id": "b1", "kind": "BUY_SET", "question": "Rain?",
                 "edge_pct": 2.0, "total_edge": 5, "annualized_pct": 50.0,
                 "cost_per_set": 0.98, "max_sets": 100, "edge_per_set": 0.02,
                 "legs": [{"outcome": "Yes", "ask": 0.62}, {"outcome": "No", "ask": 0.36}]},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertIn("최소 매수: 각 3주", text)     # ceil(1/0.36)
        self.assertIn("실행 가능", text)
        self.assertNotIn("차익 소멸", text)

    def test_no_buyin_without_leg_prices(self):
        payload = {
            "polymarket": [
                {"market_id": "x", "kind": "BUY_SET", "question": "Q?",
                 "edge_pct": 2.0, "total_edge": 5, "annualized_pct": 50.0},
            ],
            "cross_venue": [], "ev": [], "meta": {"source": "live"},
        }
        text, _ = compute_notification(payload, [])
        self.assertNotIn("최소 매수", text)


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
