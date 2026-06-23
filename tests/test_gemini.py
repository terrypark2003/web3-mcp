import unittest

from polymarket_arb.execution import ExecutionConfig, PolymarketExecutor
from polymarket_arb.gemini import build_signal_context, extract_text

WC_PAYLOAD = {
    "polymarket": [], "cross_venue": [], "ev": [],
    "world_cup": [
        {"market_id": "wc-arg", "side": "NO", "venue": "polymarket",
         "question": "Will Argentina win the 2026 World Cup?", "price": 0.80,
         "fair_prob": 0.85, "ev_per_contract": 0.05, "edge_pct": 6.5},
    ],
}


class TestExtractText(unittest.TestCase):
    def test_normal_response(self):
        data = {"candidates": [{"content": {"parts": [{"text": "hello "}, {"text": "world"}]}}]}
        self.assertEqual(extract_text(data), "hello world")

    def test_blocked_or_empty(self):
        self.assertIn("no answer", extract_text({"promptFeedback": {"blockReason": "SAFETY"}}))
        self.assertEqual(extract_text({}), "(no answer)")


class TestBuildContext(unittest.TestCase):
    def test_includes_world_cup_numbers(self):
        ctx = build_signal_context(WC_PAYLOAD)
        self.assertIn("World Cup value", ctx)
        self.assertIn("Argentina", ctx)
        self.assertIn("0.80", ctx)        # the real Polymarket price
        self.assertIn("0.85", ctx)        # the consensus fair prob

    def test_empty_is_explicit(self):
        ctx = build_signal_context({"polymarket": [], "cross_venue": [], "ev": [], "world_cup": []})
        self.assertIn("No flagged opportunities", ctx)


class TestBotAsk(unittest.TestCase):
    def _bot(self, gemini=None, wc=None):
        from polymarket_arb.bot_core import ArbBot

        cfg = ExecutionConfig.from_env({})
        return ArbBot(
            owner_id=1,
            scan_fn=lambda: [],
            executor=PolymarketExecutor(cfg),
            exec_config=cfg,
            wc_scan_fn=(lambda: wc) if wc is not None else None,
            gemini_generate=gemini,
        )

    def test_not_configured(self):
        out = self._bot().handle(1, "/ask what looks good?")
        self.assertIn("not configured", out)

    def test_usage_when_no_question(self):
        out = self._bot(gemini=lambda u, s: "x").handle(1, "/ask")
        self.assertIn("Usage", out)

    def test_passes_real_data_context_to_gemini(self):
        captured = {}

        def fake_gemini(user, system):
            captured["user"] = user
            captured["system"] = system
            return "Argentina NO looks like value (edge +6.5%). Not risk-free."

        # provide a world-cup value op so context isn't empty
        from polymarket_arb.ev import EVOpportunity

        wc = [EVOpportunity(
            kind="POSITIVE_EV", market_id="wc-arg",
            question="Will Argentina win the 2026 World Cup?", venue="polymarket",
            side="NO", price=0.80, fair_prob=0.85, ev_per_contract=0.05,
            edge_pct=6.5, max_size=1000, end_date=None,
        )]
        out = self._bot(gemini=fake_gemini, wc=wc).handle(1, "/ask whats good?")
        self.assertIn("Argentina", out)
        self.assertIn("Argentina", captured["user"])   # real data in the prompt
        self.assertIn("whats good?", captured["user"])  # the question
        self.assertIn("NEVER invent", captured["system"])  # the guardrail system prompt

    def test_gemini_error_is_caught(self):
        def boom(u, s):
            raise RuntimeError("api down")

        out = self._bot(gemini=boom).handle(1, "/ask hi")
        self.assertIn("Gemini error", out)


if __name__ == "__main__":
    unittest.main()
