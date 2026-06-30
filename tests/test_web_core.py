import unittest

from polymarket_arb.execution import ExecutionConfig, PolymarketExecutor
from polymarket_arb.models import ARB_BUY_SET, ARB_MINT_SELL, Leg, Level, Opportunity
from polymarket_arb.web_core import AuthError, DashboardError, DashboardService

TOKEN = "s3cret"


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


def make_service(ops, mode="dry-run", token=TOKEN, cross=None, ev=None):
    cfg = ExecutionConfig.from_env({"EXECUTION_MODE": mode, "MAX_STAKE_USDC": "100"})
    return DashboardService(
        scan_fn=lambda: ops,
        executor=PolymarketExecutor(cfg),
        exec_config=cfg,
        cross_scan_fn=(lambda: cross) if cross is not None else None,
        ev_scan_fn=(lambda: ev) if ev is not None else None,
        auth_token=token,
    )


class TestAuth(unittest.TestCase):
    def test_valid_token(self):
        self.assertTrue(make_service([]).check_auth(TOKEN))

    def test_wrong_token(self):
        self.assertFalse(make_service([]).check_auth("nope"))

    def test_no_token_configured_denies_all(self):
        svc = make_service([], token=None)
        self.assertFalse(svc.check_auth(""))
        self.assertFalse(svc.check_auth("anything"))

    def test_require_auth_raises(self):
        with self.assertRaises(AuthError):
            make_service([]).require_auth("bad")


class TestRead(unittest.TestCase):
    def test_status_dry_run(self):
        s = make_service([], mode="dry-run").status()
        self.assertEqual(s["mode"], "dry-run")
        self.assertEqual(s["executable_kind"], ARB_BUY_SET)

    def test_status_flags_missing_creds_in_live(self):
        s = make_service([], mode="live").status()
        self.assertFalse(s["live_ready"])
        # Only the signing key is strictly required (L2 creds derive from it).
        self.assertIn("POLYMARKET_PRIVATE_KEY", s["missing_creds"])

    def test_opportunities_groups_three_feeds(self):
        svc = make_service([buy_set_op("abc")])
        o = svc.opportunities()
        self.assertEqual(o["polymarket"][0]["market_id"], "abc")
        self.assertEqual(o["cross_venue"], [])
        self.assertEqual(o["ev"], [])

    def test_status_reports_enabled_feeds(self):
        svc = make_service([], cross=[], ev=[])
        s = svc.status()
        self.assertTrue(s["cross_enabled"])
        self.assertTrue(s["ev_enabled"])


class TestStageConfirm(unittest.TestCase):
    def test_stage_returns_plan_and_preview(self):
        svc = make_service([buy_set_op("abc")])
        r = svc.stage("abc")
        self.assertIn("stage_id", r)
        self.assertEqual(r["plan"]["market_id"], "abc")
        self.assertFalse(r["will_place_live"])
        self.assertIn("DRY-RUN", r["preview"])
        # both legs present in the plan
        self.assertEqual(len(r["plan"]["legs"]), 2)

    def test_stage_unknown_id(self):
        with self.assertRaises(DashboardError):
            make_service([buy_set_op("abc")]).stage("zzz")

    def test_stage_rejects_non_buyset(self):
        with self.assertRaises(DashboardError):
            make_service([mint_sell_op("m2")]).stage("m2")

    def test_stage_respects_custom_stake(self):
        svc = make_service([buy_set_op("abc")])
        r = svc.stage("abc", max_stake=1.0)
        # 1 USDC / 0.98 cost-per-set ~= 1.02 sets, far below the 90-set depth cap.
        self.assertLess(r["plan"]["sets"], 2)

    def test_confirm_dry_run_places_nothing(self):
        svc = make_service([buy_set_op("abc")])
        stage_id = svc.stage("abc")["stage_id"]
        r = svc.confirm(stage_id)
        self.assertTrue(r["dry_run"])
        self.assertFalse(r["placed"])

    def test_confirm_consumes_stage(self):
        svc = make_service([buy_set_op("abc")])
        stage_id = svc.stage("abc")["stage_id"]
        svc.confirm(stage_id)
        with self.assertRaises(DashboardError):
            svc.confirm(stage_id)  # already consumed

    def test_confirm_unknown_stage(self):
        with self.assertRaises(DashboardError):
            make_service([]).confirm("deadbeef")

    def test_cancel(self):
        svc = make_service([buy_set_op("abc")])
        stage_id = svc.stage("abc")["stage_id"]
        self.assertTrue(svc.cancel(stage_id)["cancelled"])
        self.assertFalse(svc.cancel(stage_id)["cancelled"])  # idempotent


if __name__ == "__main__":
    unittest.main()
