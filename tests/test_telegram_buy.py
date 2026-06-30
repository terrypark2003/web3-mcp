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


def fav(market_id="m1", outcome="Yes", price=0.92, token_id="tok-1"):
    return FavoriteBet(
        market_id=market_id, question="Will it rain tomorrow?", outcome=outcome,
        price=price, payout_multiple=1 / price, implied_prob=price, max_size=100,
        end_date="2026-07-01T00:00:00Z", days_to_resolution=1.0,
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


class TestPollFavorites(unittest.TestCase):
    def test_returns_message_and_buttons(self):
        bot = make_bot([fav(outcome="Yes", price=0.92)])
        result = bot.poll_favorites()
        self.assertIsNotNone(result)
        msg, rows = result
        self.assertIn("유력후보", msg)
        self.assertIn("무위험 아님", msg)
        self.assertEqual(len(rows), 1)
        label, data = rows[0][0]
        self.assertTrue(data.startswith("f:"))
        self.assertIn("$1 매수", label)

    def test_dedups_until_new(self):
        f = fav()
        bot = make_bot([f])
        self.assertIsNotNone(bot.poll_favorites())
        self.assertIsNone(bot.poll_favorites())  # same favorite -> nothing new

    def test_cap_lets_rest_surface_next_poll(self):
        favs = [fav(market_id=f"m{i}", token_id=f"t{i}") for i in range(5)]
        bot = make_bot(favs)
        _, rows1 = bot.poll_favorites(limit=2)
        self.assertEqual(len(rows1), 2)
        _, rows2 = bot.poll_favorites(limit=2)
        self.assertEqual(len(rows2), 2)  # the next two, not re-offering the first two


class TestBuyCallback(unittest.TestCase):
    def test_unauthorized(self):
        bot = make_bot([fav()])
        reply, rows = bot.handle_callback(STRANGER, "f:1")
        self.assertEqual(reply, "Unauthorized.")

    def test_tap_stages_then_confirm_executes_dry_run(self):
        bot = make_bot([fav(outcome="Yes", price=0.92)])
        _, rows = bot.poll_favorites()
        ref = rows[0][0][1]                          # callback_data like "f:1"
        staged, buttons = bot.handle_callback(OWNER, ref)
        self.assertIn("DRY-RUN", staged)             # shows what would be sent
        self.assertIn("무위험 아님", staged)
        self.assertIn(OWNER, bot._pending)           # plan staged
        self.assertEqual(buttons[0][0][1], "fav_confirm")
        # Confirm -> executes (dry-run places nothing)
        done, _ = bot.handle_callback(OWNER, "fav_confirm")
        self.assertIn("NO ORDERS PLACED", done)
        self.assertNotIn(OWNER, bot._pending)        # cleared after confirm

    def test_cancel_clears_pending(self):
        bot = make_bot([fav()])
        _, rows = bot.poll_favorites()
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
