import unittest

from polymarket_arb.execution import (
    DRY_RUN,
    LIVE,
    ExecutionConfig,
    ExecutionError,
    PolymarketExecutor,
    build_order_plan,
    simulate,
)
from polymarket_arb.models import ARB_BUY_SET, ARB_MINT_SELL, Leg, Level, Opportunity


def buy_set_op(market_id="m1"):
    legs = [
        Leg("yes", "Yes", Level(0.62, 120), Level(0.60, 150)),
        Leg("no", "No", Level(0.36, 90), Level(0.34, 80)),
    ]
    return Opportunity(
        kind=ARB_BUY_SET, market_id=market_id, question="Will it rain?",
        neg_risk=False, exhaustive=True, end_date="2026-07-02T00:00:00Z",
        n_legs=2, cost_per_set=0.98, proceeds_per_set=1.0, edge_per_set=0.02,
        edge_pct=2.04, max_sets=90, capital_required=88.2, total_edge=1.8,
        annualized_pct=55.0, legs=legs,
    )


class TestBuildOrderPlan(unittest.TestCase):
    def test_depth_caps_size(self):
        plan = build_order_plan(buy_set_op(), max_stake=100.0, slippage=0.0)
        self.assertAlmostEqual(plan.sets, 90.0, places=6)
        self.assertEqual([leg.side for leg in plan.legs], ["BUY", "BUY"])
        self.assertAlmostEqual(plan.legs[0].price, 0.62, places=6)
        self.assertAlmostEqual(plan.total_cost, 88.2, places=6)
        self.assertAlmostEqual(plan.expected_profit, 1.8, places=6)

    def test_stake_caps_size(self):
        plan = build_order_plan(buy_set_op(), max_stake=0.98, slippage=0.0)
        self.assertAlmostEqual(plan.sets, 1.0, places=6)

    def test_slippage_raises_limit_price(self):
        plan = build_order_plan(buy_set_op(), max_stake=100.0, slippage=0.01)
        self.assertAlmostEqual(plan.legs[0].price, 0.62 * 1.01, places=6)

    def test_rejects_mint_sell(self):
        op = buy_set_op()
        op.kind = ARB_MINT_SELL
        with self.assertRaises(ExecutionError):
            build_order_plan(op, max_stake=1.0)

    def test_rejects_missing_ask(self):
        op = buy_set_op()
        op.legs[1].best_ask = None
        with self.assertRaises(ExecutionError):
            build_order_plan(op, max_stake=1.0)


class TestExecutionConfig(unittest.TestCase):
    def test_defaults_to_dry_run(self):
        cfg = ExecutionConfig.from_env({})
        self.assertEqual(cfg.mode, DRY_RUN)
        self.assertEqual(cfg.max_stake, 1.0)

    def test_unknown_mode_falls_back_to_dry_run(self):
        cfg = ExecutionConfig.from_env({"EXECUTION_MODE": "yolo"})
        self.assertEqual(cfg.mode, DRY_RUN)

    def test_live_ready_reports_missing(self):
        cfg = ExecutionConfig.from_env({"EXECUTION_MODE": "live"})
        ready, missing = cfg.live_ready()
        self.assertFalse(ready)
        # Only the signing key is strictly required; L2 creds derive from it.
        self.assertEqual(missing, ["POLYMARKET_PRIVATE_KEY"])

    def test_live_ready_true_with_only_private_key(self):
        cfg = ExecutionConfig.from_env({
            "EXECUTION_MODE": "live", "POLYMARKET_PRIVATE_KEY": "0xabc",
        })
        self.assertEqual(cfg.live_ready(), (True, []))   # creds derived at connect
        self.assertFalse(cfg.has_explicit_api_creds)

    def test_signature_type_parsed_from_env(self):
        cfg = ExecutionConfig.from_env({
            "POLYMARKET_PRIVATE_KEY": "0xabc",
            "POLYMARKET_FUNDER": "0xproxy",
            "POLYMARKET_SIGNATURE_TYPE": "1",
        })
        self.assertEqual(cfg.signature_type, 1)
        self.assertEqual(cfg.funder, "0xproxy")

    def test_signature_type_absent_is_none(self):
        cfg = ExecutionConfig.from_env({"POLYMARKET_PRIVATE_KEY": "0xabc"})
        self.assertIsNone(cfg.signature_type)

    def test_relayer_key_parsed_from_env(self):
        cfg = ExecutionConfig.from_env({
            "POLYMARKET_RELAYER_API_KEY": "rk-123",
            "POLYMARKET_RELAYER_ADDRESS": "0xrelayer",
        })
        self.assertEqual(cfg.relayer_api_key, "rk-123")
        self.assertEqual(cfg.relayer_address, "0xrelayer")

    def test_relayer_key_absent_is_none(self):
        cfg = ExecutionConfig.from_env({})
        self.assertIsNone(cfg.relayer_api_key)
        self.assertIsNone(cfg.relayer_address)

    def test_live_ready_true_with_all_creds(self):
        cfg = ExecutionConfig.from_env({
            "EXECUTION_MODE": "live",
            "POLYMARKET_API_KEY": "k",
            "POLYMARKET_API_SECRET": "s",
            "POLYMARKET_API_PASSPHRASE": "p",
            "POLYMARKET_PRIVATE_KEY": "0xabc",
        })
        self.assertEqual(cfg.mode, LIVE)
        self.assertEqual(cfg.live_ready(), (True, []))


class TestExecutorSafety(unittest.TestCase):
    def test_dry_run_places_nothing(self):
        cfg = ExecutionConfig.from_env({})  # dry-run
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        result = PolymarketExecutor(cfg).execute(plan)
        self.assertFalse(result.placed)
        self.assertTrue(result.dry_run)
        self.assertIn("주문 안 함", result.detail)

    def test_live_without_creds_raises(self):
        cfg = ExecutionConfig.from_env({"EXECUTION_MODE": "live"})
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        with self.assertRaises(ExecutionError):
            PolymarketExecutor(cfg).execute(plan)

    def test_simulate_text(self):
        cfg = ExecutionConfig.from_env({})
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        text = simulate(plan, cfg)
        self.assertIn("드라이런", text)
        self.assertIn("주문 안 함", text)


class _StubClient:
    """Stands in for polymarket-client's SecureClient (place_limit_order only)."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def place_limit_order(self, **kwargs):
        self.calls.append(kwargs)
        return self.resp


class _Accepted:
    ok = True
    order_id = "0xorder"
    status = "matched"


class _Rejected:
    ok = False
    code = "not_enough_balance"
    message = "not enough balance / allowance"


class TestLiveExecuteWithStubClient(unittest.TestCase):
    """The live path is now stub-testable: execute() only calls place_limit_order
    and branches on resp.ok — no SDK imports inside the loop."""

    def _executor(self, resp):
        cfg = ExecutionConfig.from_env({
            "EXECUTION_MODE": "live", "POLYMARKET_PRIVATE_KEY": "0xabc",
        })
        ex = PolymarketExecutor(cfg)
        ex._client = _StubClient(resp)   # bypass network client construction
        return ex

    def test_accepted_marks_placed_and_sends_buy_legs(self):
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        ex = self._executor(_Accepted())
        result = ex.execute(plan)
        self.assertTrue(result.placed)
        self.assertEqual(len(ex._client.calls), len(plan.legs))
        for call in ex._client.calls:
            self.assertEqual(call["side"], "BUY")
            self.assertIn("token_id", call)
            self.assertGreater(call["size"], 0)

    def test_rejected_reports_code_and_does_not_continue(self):
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        ex = self._executor(_Rejected())
        result = ex.execute(plan)
        self.assertFalse(result.placed)
        self.assertIn("not_enough_balance", result.detail)   # actionable error code
        self.assertEqual(len(ex._client.calls), 1)           # stopped at first reject


if __name__ == "__main__":
    unittest.main()
