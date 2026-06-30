import unittest

from polymarket_arb.bot_core import ArbBot
from polymarket_arb.execution import (
    FAV_BUY,
    ExecutionConfig,
    ExecutionError,
    PolymarketExecutor,
    build_single_buy_plan,
)
from polymarket_arb.favorites import FavoriteBet

OWNER = 4242
STRANGER = 9999


def fav(market_id="m1", outcome="Yes", price=0.92, token_id="tok-1", hours=2.0):
    return FavoriteBet(
        market_id=market_id, question="Will it rain tomorrow?", outcome=outcome,
        price=price, payout_multiple=1 / price, implied_prob=price, max_size=100,
        end_date="2026-07-01T00:00:00Z", days_to_resolution=hours / 24.0,
        url="https://polymarket.com/event/x", token_id=token_id,
    )


def make_bot(favs, mode="dry-run"):
    cfg = ExecutionConfig.from_env({"EXECUTION_MODE": mode, "SLIPPAGE": "0.01"})
    return ArbBot(
        owner_id=OWNER,
        scan_fn=lambda: [],
        executor=PolymarketExecutor(cfg),
        exec_config=cfg,
        fav_scan_fn=lambda: favs,
        fav_max_buy_usd=1.0,
    )


class TestSingleBuyPlan(unittest.TestCase):
    def test_sizes_one_dollar(self):
        plan = build_single_buy_plan("tok", "Yes", 0.90, dollars=1.0, slippage=0.01)
        self.assertEqual(plan.kind, FAV_BUY)
        self.assertEqual(len(plan.legs), 1)
        self.assertAlmostEqual(plan.sets, round(1.0 / 0.90, 2))            # ~1.11 shares
        self.assertAlmostEqual(plan.legs[0].price, min(1.0, 0.90 * 1.01))  # ask + slippage
        # If it wins each share pays $1; small profit, small stake.
        self.assertAlmostEqual(plan.expected_payoff, plan.sets)

    def test_rejects_bad_price(self):
        with self.assertRaises(ExecutionError):
            build_single_buy_plan("tok", "Yes", 0.0)


class TestFavoritesNow(unittest.TestCase):
    def test_returns_message_buttons_and_eta(self):
        bot = make_bot([fav(outcome="Yes", price=0.92, hours=2.0)])
        chunks = bot.favorites_now()
        self.assertEqual(len(chunks), 1)
        msg, rows = chunks[0]
        self.assertIn("3시간", msg)        # ≤3h bucket header
        self.assertIn("⏳2.0h", msg)        # ETA shown
        self.assertIn("무위험 아님", msg)
        self.assertEqual(len(rows), 1)
        label, data = rows[0][0]
        self.assertTrue(data.startswith("f:"))
        self.assertTrue(label.startswith("1)"))

    def test_buckets_by_hours(self):
        favs = [fav(market_id=f"m{i}", token_id=f"t{i}", hours=h)
                for i, h in enumerate([2, 5, 8, 11])]
        chunks = make_bot(favs).favorites_now()
        headers = [msg.splitlines()[0] for msg, _ in chunks]
        self.assertEqual(len(chunks), 4)   # one per bucket
        self.assertTrue(any("3시간" in h for h in headers))
        self.assertTrue(any("12시간" in h for h in headers))

    def test_chunks_of_five_within_bucket(self):
        favs = [fav(market_id=f"m{i}", token_id=f"t{i}", hours=2.0) for i in range(7)]
        chunks = make_bot(favs).favorites_now()  # all in ≤3h -> 5 + 2
        self.assertEqual([len(rows) for _, rows in chunks], [5, 2])

    def test_excludes_beyond_12h(self):
        chunks = make_bot([fav(hours=20.0)]).favorites_now()
        self.assertEqual(chunks, [])

    def test_not_deduped(self):
        bot = make_bot([fav(hours=2.0)])
        self.assertTrue(bot.favorites_now())
        self.assertTrue(bot.favorites_now())  # on-demand: lists again, not deduped


class TestBuyCallback(unittest.TestCase):
    def test_unauthorized(self):
        bot = make_bot([fav()])
        reply, rows = bot.handle_callback(STRANGER, "f:1")
        self.assertEqual(reply, "Unauthorized.")

    def test_tap_stages_then_confirm_executes_dry_run(self):
        bot = make_bot([fav(outcome="Yes", price=0.92)])
        rows = bot.favorites_now()[0][1]
        ref = rows[0][0][1]                          # callback_data like "f:1"
        staged, buttons = bot.handle_callback(OWNER, ref)
        self.assertIn("드라이런", staged)             # shows what would be sent
        self.assertIn("무위험 아님", staged)
        self.assertIn(OWNER, bot._pending)           # plan staged
        self.assertEqual(buttons[0][0][1], "fav_confirm")
        # Confirm -> executes (dry-run places nothing)
        done, _ = bot.handle_callback(OWNER, "fav_confirm")
        self.assertIn("주문 안 함", done)
        self.assertNotIn(OWNER, bot._pending)        # cleared after confirm

    def test_cancel_clears_pending(self):
        bot = make_bot([fav()])
        rows = bot.favorites_now()[0][1]
        bot.handle_callback(OWNER, rows[0][0][1])
        self.assertIn(OWNER, bot._pending)
        reply, _ = bot.handle_callback(OWNER, "fav_cancel")
        self.assertIn("취소", reply)
        self.assertNotIn(OWNER, bot._pending)

    def test_expired_ref(self):
        bot = make_bot([fav()])
        reply, _ = bot.handle_callback(OWNER, "f:999")
        self.assertIn("만료", reply)


if __name__ == "__main__":
    unittest.main()
