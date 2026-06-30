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
    def test_one_message_with_eta(self):
        bot = make_bot([fav(outcome="Yes", price=0.92, hours=2.0)])
        chunks = bot.favorites_now()
        self.assertEqual(len(chunks), 1)             # always a single message
        msg, rows = chunks[0]
        self.assertIn("시간 남음", msg)              # ETA always shown
        self.assertIn("무위험 아님", msg)
        self.assertEqual(len(rows), 1)
        label, data = rows[0][0]
        self.assertTrue(data.startswith("http"))     # opens the market (manual buy)
        self.assertTrue(label.startswith("1)"))

    def test_limits_to_five(self):
        favs = [fav(market_id=f"m{i}", token_id=f"t{i}", hours=2.0 + i * 0.01)
                for i in range(8)]
        chunks = make_bot(favs).favorites_now()
        self.assertEqual(len(chunks), 1)
        rows = chunks[0][1]
        item_rows = [r for r in rows if r[0][1].startswith("http")]
        self.assertEqual(len(item_rows), 5)          # 5 items per page
        self.assertEqual(rows[-1][0][1], "fav_more")  # + a 더보기 button (3 remain)

    def test_more_button_pages_next_five(self):
        favs = [fav(market_id=f"m{i}", token_id=f"t{i}", hours=2.0 + i * 0.01)
                for i in range(8)]
        bot = make_bot(favs)
        msg1, rows1 = bot.favorites_now()[0]
        self.assertEqual(rows1[-1][0][1], "fav_more")               # page 1 -> 더보기
        self.assertEqual(len([r for r in rows1 if r[0][1].startswith("http")]), 5)
        # Tap 더보기 -> the remaining 3, numbered 6.. , and no further 더보기.
        msg2, rows2 = bot.handle_callback(OWNER, "fav_more")
        self.assertEqual(len([r for r in rows2 if r[0][1].startswith("http")]), 3)
        self.assertNotIn("fav_more", [r[0][1] for r in rows2])
        self.assertIn("6)", msg2)                                   # numbering continues
        # Nothing left to page.
        reply, _ = bot.handle_callback(OWNER, "fav_more")
        self.assertIn("더 보여줄", reply)

    def test_soonest_first(self):
        favs = [fav(market_id=f"m{i}", token_id=f"t{i}", hours=h)
                for i, h in enumerate([10, 1, 5])]
        msg = make_bot(favs).favorites_now()[0][0]
        first_item = [ln for ln in msg.splitlines() if ln.startswith("1)")][0]
        self.assertIn("1.0시간", first_item)         # soonest (1h) is item 1

    def test_excludes_beyond_12h(self):
        self.assertEqual(make_bot([fav(hours=20.0)]).favorites_now(), [])

    def test_max_hours_window(self):
        bot = make_bot([fav(hours=2.0)])
        self.assertEqual(bot.favorites_now(max_hours=1.0), [])  # 2h outside /fav1
        self.assertTrue(bot.favorites_now(max_hours=3.0))       # inside /fav3
        # header reflects the window
        self.assertIn("3시간 내", bot.favorites_now(max_hours=3.0)[0][0])

    def test_not_deduped(self):
        bot = make_bot([fav(hours=2.0)])
        self.assertTrue(bot.favorites_now())
        self.assertTrue(bot.favorites_now())  # on-demand: lists again, not deduped

    def test_button_opens_market_url(self):
        # CLOB V2 blocks API auto-buy, so the button must be a link to the market.
        bot = make_bot([fav(hours=2.0)])             # fav() url = polymarket event link
        msg, rows = bot.favorites_now()[0]
        label, value = rows[0][0]
        self.assertEqual(value, "https://polymarket.com/event/x")  # opens that market
        self.assertIn("폴리마켓에서 매수", label)      # wording = manual buy on Polymarket
        self.assertIn("직접 매수", msg)               # header explains the link flow


class TestBalance(unittest.TestCase):
    def test_balanceof_calldata(self):
        from polymarket_arb.execution import _erc20_balanceof_data
        d = _erc20_balanceof_data("0xC16CBCC9590952d72a1ff3e59854871ca9b0CB32")
        self.assertTrue(d.startswith("0x70a08231"))   # balanceOf selector
        self.assertEqual(len(d), 10 + 64)             # selector + 32-byte arg
        self.assertTrue(d.endswith("c16cbcc9590952d72a1ff3e59854871ca9b0cb32"))

    def test_hex_to_usdc(self):
        from polymarket_arb.execution import _hex_to_usdc
        self.assertAlmostEqual(_hex_to_usdc(hex(6_500_000)), 6.5)  # $6.50
        self.assertEqual(_hex_to_usdc("0x"), 0.0)
        self.assertEqual(_hex_to_usdc(None), 0.0)

    def test_balance_command_without_funder(self):
        bot = make_bot([])  # no POLYMARKET_FUNDER configured
        self.assertIn("POLYMARKET_FUNDER", bot.handle(OWNER, "/balance"))

    def test_balance_command_success(self):
        bot = make_bot([])
        bot.exec_config.funder = "0xC16CBCC9590952d72a1ff3e59854871ca9b0CB32"
        bot.executor.usdc_balance = lambda: 6.5        # stub the on-chain read
        self.assertIn("$6.50", bot.handle(OWNER, "/balance"))

    def test_balance_rotates_past_dead_rpc(self):
        # A public RPC that 401s (like polygon-rpc.com did) must not break /balance:
        # the read falls through to the next endpoint instead of erroring out.
        import requests
        from unittest import mock

        from polymarket_arb.execution import _USDC_E

        cfg = ExecutionConfig.from_env({})       # rpc_url None -> uses the public list
        cfg.funder = "0xC16CBCC9590952d72a1ff3e59854871ca9b0CB32"
        ex = PolymarketExecutor(cfg)

        class FakeResp:
            def __init__(self, status, result=None):
                self.status_code, self._result = status, result

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(f"{self.status_code} Unauthorized")

            def json(self):
                return {"jsonrpc": "2.0", "id": 1, "result": self._result}

        seen = []

        def fake_post(url, **kwargs):
            seen.append(url)
            if "publicnode" in url:              # first endpoint is down (401)
                return FakeResp(401)
            to = kwargs["json"]["params"][0]["to"]
            return FakeResp(200, hex(6_500_000) if to == _USDC_E else "0x0")

        with mock.patch("requests.post", side_effect=fake_post):
            self.assertAlmostEqual(ex.usdc_balance(), 6.5)
        self.assertTrue(any("publicnode" in u for u in seen))   # tried the dead one
        self.assertGreater(len(seen), 1)                        # then moved on

    def test_balance_all_rpcs_dead_raises(self):
        import requests
        from unittest import mock

        cfg = ExecutionConfig.from_env({})
        cfg.funder = "0xC16CBCC9590952d72a1ff3e59854871ca9b0CB32"
        ex = PolymarketExecutor(cfg)
        with mock.patch("requests.post", side_effect=requests.HTTPError("401")):
            with self.assertRaises(ExecutionError) as ctx:
                ex.usdc_balance()
        self.assertIn("POLYGON_RPC_URL", str(ctx.exception))     # actionable hint

    def test_balance_reads_configured_collateral_token(self):
        # CLOB V2 holds funds in pUSD (a different ERC-20). Setting
        # POLYMARKET_COLLATERAL_TOKEN must make /balance read that token too,
        # otherwise it reports $0 even though the site shows cash.
        import requests
        from unittest import mock

        PUSD = "0xabcabcabcabcabcabcabcabcabcabcabcabcabca"
        cfg = ExecutionConfig.from_env({"POLYMARKET_COLLATERAL_TOKEN": PUSD})
        self.assertEqual(cfg.collateral_token, PUSD)
        cfg.funder = "0xC16CBCC9590952d72a1ff3e59854871ca9b0CB32"
        ex = PolymarketExecutor(cfg)

        queried = []

        class FakeResp:
            def __init__(self, result):
                self._result = result

            def raise_for_status(self):
                pass

            def json(self):
                return {"result": self._result}

        def fake_post(url, **kwargs):
            to = kwargs["json"]["params"][0]["to"]
            queried.append(to)
            return FakeResp(hex(6_500_000) if to == PUSD else "0x0")

        with mock.patch("requests.post", side_effect=fake_post):
            self.assertAlmostEqual(ex.usdc_balance(), 6.5)   # pUSD balance counted
        self.assertIn(PUSD, queried)                          # the pUSD token was read


class TestBuyCallback(unittest.TestCase):
    def test_unauthorized(self):
        bot = make_bot([fav()])
        reply, rows = bot.handle_callback(STRANGER, "f:1")
        self.assertEqual(reply, "Unauthorized.")

    def test_tap_stages_then_confirm_executes_dry_run(self):
        bot = make_bot([fav(outcome="Yes", price=0.92)])
        bot.favorites_now()                          # registers f:1 (engine kept alive)
        ref = "f:1"                                  # auto-buy path still works if wired
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
        bot.favorites_now()                          # registers f:1
        bot.handle_callback(OWNER, "f:1")
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
