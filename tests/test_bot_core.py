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
        self.assertIn("주문 안 함", out)

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
        self.assertIn("주문 안 함", result)

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


def cross_op(event_id="btc", edge_pct=2.35, total_edge=18.0):
    from polymarket_arb.crossvenue import ARB_CROSS_VENUE, CrossVenueOpportunity

    return CrossVenueOpportunity(
        kind=ARB_CROSS_VENUE, event_id=event_id, question="BTC > 100k?",
        end_date=None, yes_venue="polymarket", no_venue="kalshi",
        yes_price=0.49, no_price=0.47, cost_per_set=0.96, fee_per_set=0.0174,
        edge_per_set=0.0226, edge_pct=edge_pct, max_sets=800,
        capital_required=768.0, total_edge=total_edge, annualized_pct=None,
    )


def ev_op(market_id="m", side="YES"):
    from polymarket_arb.ev import EV_SIGNAL, EVOpportunity

    return EVOpportunity(
        kind=EV_SIGNAL, market_id=market_id, question="BTC > 100k?",
        venue="polymarket", side=side, price=0.49, fair_prob=0.62,
        ev_per_contract=0.13, edge_pct=26.5, max_size=1000, end_date=None,
    )


def make_multi_bot(poly_ops=None, cross_ops=None, ev_ops=None, channel=None):
    cfg = ExecutionConfig.from_env({})
    return ArbBot(
        owner_id=OWNER,
        scan_fn=lambda: poly_ops or [],
        executor=PolymarketExecutor(cfg),
        exec_config=cfg,
        cross_scan_fn=(lambda: cross_ops) if cross_ops is not None else None,
        ev_scan_fn=(lambda: ev_ops) if ev_ops is not None else None,
        signal_channel_id=channel,
    )


class TestMultiVenueCommands(unittest.TestCase):
    def test_cross_lists_opportunities(self):
        bot = make_multi_bot(cross_ops=[cross_op("btc")])
        out = bot.handle(OWNER, "/cross")
        self.assertIn("BTC", out)
        self.assertIn("YES@polymarket", out)
        self.assertIn("NO@kalshi", out)

    def test_cross_not_configured(self):
        bot = make_multi_bot()
        self.assertIn("not configured", bot.handle(OWNER, "/cross"))

    def test_cross_empty(self):
        bot = make_multi_bot(cross_ops=[])
        self.assertIn("No cross-venue", bot.handle(OWNER, "/cross"))

    def test_ev_lists_and_warns_not_risk_free(self):
        bot = make_multi_bot(ev_ops=[ev_op("m")])
        out = bot.handle(OWNER, "/ev")
        self.assertIn("NOT risk-free", out)
        self.assertIn("YES@polymarket", out)

    def test_ev_not_configured(self):
        bot = make_multi_bot()
        self.assertIn("not configured", bot.handle(OWNER, "/ev"))

    def test_help_lists_new_commands(self):
        bot = make_multi_bot()
        out = bot.handle(OWNER, "/help")
        self.assertIn("/cross", out)
        self.assertIn("/ev", out)


class TestBroadcast(unittest.TestCase):
    def test_aggregates_poly_and_cross_then_dedups(self):
        bot = make_multi_bot(poly_ops=[buy_set_op("p1")], cross_ops=[cross_op("c1")])
        first = bot.poll_broadcast()
        self.assertIsNotNone(first)
        self.assertIn("p1", first)
        self.assertIn("Cross-venue", first)
        self.assertIsNone(bot.poll_broadcast())  # nothing new second time

    def test_excludes_ev_from_autofeed(self):
        # EV configured, but poll_broadcast must not surface EV signals.
        bot = make_multi_bot(cross_ops=[cross_op("c1")], ev_ops=[ev_op("m")])
        out = bot.poll_broadcast()
        self.assertNotIn("fair", out)  # EV lines mention 'fair'; arbs don't

    def test_new_cross_fires_after_first(self):
        ops = [cross_op("c1")]
        bot = make_multi_bot(cross_ops=ops)
        bot.poll_broadcast()
        ops.append(cross_op("c2"))
        out = bot.poll_broadcast()
        self.assertIsNotNone(out)
        self.assertIn("Cross-venue", out)


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
