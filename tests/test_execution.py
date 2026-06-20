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
        self.assertIn("POLYMARKET_API_KEY", missing)

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
        self.assertIn("NO ORDERS PLACED", result.detail)

    def test_live_without_creds_raises(self):
        cfg = ExecutionConfig.from_env({"EXECUTION_MODE": "live"})
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        with self.assertRaises(ExecutionError):
            PolymarketExecutor(cfg).execute(plan)

    def test_simulate_text(self):
        cfg = ExecutionConfig.from_env({})
        plan = build_order_plan(buy_set_op(), max_stake=1.0)
        text = simulate(plan, cfg)
        self.assertIn("DRY-RUN", text)
        self.assertIn("NO ORDERS PLACED", text)


if __name__ == "__main__":
    unittest.main()
