import unittest

from polymarket_arb.settlements import (
    LOSS,
    SETTLED,
    WIN,
    check_settlements,
    format_settlement_message,
    position_key,
)


def pos(cond="c1", outcome="Yes", cur_price=0.5, value=10.0, pnl=0.0, redeemable=False,
        title="Will it rain?"):
    return {
        "conditionId": cond, "outcome": outcome, "curPrice": cur_price,
        "currentValue": value, "cashPnl": pnl, "redeemable": redeemable, "title": title,
    }


class TestCheckSettlements(unittest.TestCase):
    def test_open_position_tracked_no_event(self):
        events, state = check_settlements([pos(cur_price=0.6)], {})
        self.assertEqual(events, [])
        self.assertIn(position_key(pos()), state["open"])

    def test_cold_start_already_resolved_is_silent(self):
        # First time ever seeing this key, and it's already resolved: no
        # backfill spam, but it's recorded so it won't fire on the next poll.
        events, state = check_settlements([pos(cur_price=1.0, redeemable=True)], {})
        self.assertEqual(events, [])
        self.assertIn(position_key(pos()), state["notified"])

    def test_open_then_win(self):
        _, state = check_settlements([pos(cur_price=0.6)], {})
        events, state = check_settlements(
            [pos(cur_price=1.0, redeemable=True, value=20.0, pnl=8.0)], state,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], WIN)
        self.assertEqual(events[0]["pnl"], 8.0)

    def test_open_then_loss(self):
        _, state = check_settlements([pos(cur_price=0.6)], {})
        events, state = check_settlements(
            [pos(cur_price=0.0, redeemable=False, value=0.0, pnl=-10.0)], state,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], LOSS)

    def test_no_duplicate_notification(self):
        _, state = check_settlements([pos(cur_price=0.6)], {})
        events1, state = check_settlements([pos(cur_price=1.0, redeemable=True)], state)
        self.assertEqual(len(events1), 1)
        # Position still shows up (not yet manually cleared) -> must not refire.
        events2, state = check_settlements([pos(cur_price=1.0, redeemable=True)], state)
        self.assertEqual(events2, [])

    def test_vanished_open_position_is_settled_win(self):
        _, state = check_settlements([pos(cur_price=0.6, value=10.0, pnl=2.0)], {})
        events, state = check_settlements([], state)  # redeemed & cleared from wallet
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], SETTLED)
        self.assertNotIn(position_key(pos()), state["open"])

    def test_distinct_outcomes_tracked_independently(self):
        _, state = check_settlements(
            [pos(outcome="Yes", cur_price=0.6), pos(outcome="No", cur_price=0.4)], {},
        )
        events, _ = check_settlements(
            [pos(outcome="Yes", cur_price=1.0, redeemable=True),
             pos(outcome="No", cur_price=0.4)],
            state,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "Yes")


class TestFormatSettlementMessage(unittest.TestCase):
    def test_win_message(self):
        msg = format_settlement_message(
            {"kind": WIN, "title": "Will it rain?", "outcome": "Yes", "value": 20.0, "pnl": 8.0},
        )
        self.assertIn("적중", msg)
        self.assertIn("$20.00", msg)
        self.assertIn("+$8.00", msg)

    def test_loss_message(self):
        msg = format_settlement_message(
            {"kind": LOSS, "title": "Will it rain?", "outcome": "Yes", "value": 0.0, "pnl": -10.0},
        )
        self.assertIn("낙첨", msg)
        self.assertIn("-$10.00", msg)


if __name__ == "__main__":
    unittest.main()
