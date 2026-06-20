import unittest

from polymarket_arb.bot_core import ArbBot
from polymarket_arb.execution import ExecutionConfig, PolymarketExecutor
from polymarket_arb.models import ARB_BUY_SET, ARB_MINT_SELL, Leg, Level, Opportunity

OWNER = 4242
STRANGER = 9999


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


def mint_sell_op(market_id="m2"):
    op = buy_set_op(market_id)
    op.kind = ARB_MINT_SELL
    return op


def make_bot(ops, mode="dry-run", env_extra=None):
    env = {"EXECUTION_MODE": mode}
    env.update(env_extra or {})
    cfg = ExecutionConfig.from_env(env)
    return ArbBot(
        owner_id=OWNER,
        scan_fn=lambda: ops,
        executor=PolymarketExecutor(cfg),
        exec_config=cfg,
    )


class TestAuth(unittest.TestCase):
    def test_stranger_is_rejected(self):
        bot = make_bot([buy_set_op()])
        self.assertEqual(bot.handle(STRANGER, "/scan"), "Unauthorized.")

    def test_owner_help(self):
        bot = make_bot([])
        self.assertIn("/scan", bot.handle(OWNER, "/help"))


class TestCommands(unittest.TestCase):
    def test_scan_lists_ids(self):
        bot = make_bot([buy_set_op("abc")])
        out = bot.handle(OWNER, "/scan")
        self.assertIn("abc", out)

    def test_scan_empty(self):
        bot = make_bot([])
        self.assertIn("No arbitrage", bot.handle(OWNER, "/scan"))

    def test_allocate(self):
        bot = make_bot([buy_set_op()])
        out = bot.handle(OWNER, "/allocate 1000")
        self.assertIn("Bankroll", out)

    def test_allocate_needs_number(self):
        bot = make_bot([buy_set_op()])
        self.assertIn("Not a number", bot.handle(OWNER, "/allocate abc"))

    def test_plan_dry_run(self):
        bot = make_bot([buy_set_op("abc")])
        out = bot.handle(OWNER, "/plan abc")
        self.assertIn("NO ORDERS PLACED", out)

    def test_plan_unknown_id(self):
        bot = make_bot([buy_set_op("abc")])
        self.assertIn("No current opportunity", bot.handle(OWNER, "/plan zzz"))

    def test_mint_sell_not_executable(self):
        bot = make_bot([mint_sell_op("ms")])
        self.assertIn("not executable", bot.handle(OWNER, "/plan ms"))

    def test_execute_then_confirm_dry_run(self):
        bot = make_bot([buy_set_op("abc")])
        staged = bot.handle(OWNER, "/execute abc")
        self.assertIn("/confirm", staged)
        result = bot.handle(OWNER, "/confirm")
        self.assertIn("NO ORDERS PLACED", result)

    def test_confirm_without_stage(self):
        bot = make_bot([buy_set_op()])
        self.assertIn("Nothing staged", bot.handle(OWNER, "/confirm"))

    def test_cancel_clears_stage(self):
        bot = make_bot([buy_set_op("abc")])
        bot.handle(OWNER, "/execute abc")
        self.assertIn("discarded", bot.handle(OWNER, "/cancel"))
        self.assertIn("Nothing staged", bot.handle(OWNER, "/confirm"))

    def test_status_dry_run_note(self):
        bot = make_bot([])
        self.assertIn("Dry-run", bot.handle(OWNER, "/status"))

    def test_status_live_missing_creds_warns(self):
        bot = make_bot([], mode="live")
        out = bot.handle(OWNER, "/status")
        self.assertIn("missing creds", out)


class TestAlerts(unittest.TestCase):
    def test_first_poll_fires_then_dedups(self):
        op = buy_set_op("abc")
        bot = make_bot([op])
        first = bot.poll_alerts()
        self.assertIsNotNone(first)
        self.assertIn("abc", first)
        # Same opportunity still present -> no repeat alert.
        self.assertIsNone(bot.poll_alerts())

    def test_new_opportunity_fires(self):
        ops = [buy_set_op("abc")]
        bot = make_bot(ops)
        bot.poll_alerts()  # sees abc
        ops.append(buy_set_op("def"))  # def appears
        out = bot.poll_alerts()
        self.assertIsNotNone(out)
        self.assertIn("def", out)
        self.assertNotIn("abc", out)

    def test_disappear_then_reappear_refires(self):
        ops = [buy_set_op("abc")]
        bot = make_bot(ops)
        bot.poll_alerts()        # sees abc
        ops.clear()
        self.assertIsNone(bot.poll_alerts())  # gone
        ops.append(buy_set_op("abc"))         # back
        self.assertIsNotNone(bot.poll_alerts())

    def test_alerts_off_silences(self):
        bot = make_bot([buy_set_op("abc")])
        self.assertIn("OFF", bot.handle(OWNER, "/alerts off"))
        self.assertIsNone(bot.poll_alerts())
        self.assertIn("ON", bot.handle(OWNER, "/alerts on"))
        self.assertIsNotNone(bot.poll_alerts())

    def test_min_edge_filters_alerts(self):
        # buy_set_op edge_pct is 2.04; threshold 3.0 should suppress it.
        cfg = ExecutionConfig.from_env({})
        bot = ArbBot(
            owner_id=OWNER,
            scan_fn=lambda: [buy_set_op("abc")],
            executor=PolymarketExecutor(cfg),
            exec_config=cfg,
            min_alert_edge_pct=3.0,
        )
        self.assertIsNone(bot.poll_alerts())


if __name__ == "__main__":
    unittest.main()
