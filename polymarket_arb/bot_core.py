"""Telegram bot command logic — pure and testable.

This module has no Telegram or network dependency: it takes a chat id and a
command string and returns a reply string. The Telegram wiring in
``telegram_bot.py`` is a thin async shell over ``ArbBot.handle``. Opportunity
discovery is injected as ``scan_fn`` so it can be stubbed in tests (and so the
real bot supplies the live scanner).
"""

from __future__ import annotations

from typing import Callable, Optional

from .detect import FeeModel
from .execution import (
    ExecutionConfig,
    OrderPlan,
    PolymarketExecutor,
    build_order_plan,
)
from .models import ARB_BUY_SET, Opportunity
from .portfolio import SizingConfig, allocate_portfolio, format_portfolio

HELP = (
    "Polymarket arbitrage bot (owner-only)\n"
    "/scan - find current complete-set arbitrage\n"
    "/allocate <bankroll> - size bets across the edges\n"
    "/plan <market_id> - show the order plan for one opportunity\n"
    "/execute <market_id> - stage a trade, then /confirm to place it\n"
    "/confirm - execute the staged trade (live only; dry-run otherwise)\n"
    "/cancel - discard the staged trade\n"
    "/status - show mode, max stake, and live-readiness"
)


class ArbBot:
    def __init__(
        self,
        owner_id: int,
        scan_fn: Callable[[], list[Opportunity]],
        executor: PolymarketExecutor,
        exec_config: ExecutionConfig,
        fee_model: Optional[FeeModel] = None,
    ) -> None:
        self.owner_id = owner_id
        self.scan_fn = scan_fn
        self.executor = executor
        self.exec_config = exec_config
        self.fee_model = fee_model or FeeModel()
        self._pending: dict[int, OrderPlan] = {}

    def is_authorized(self, chat_id: int) -> bool:
        return chat_id == self.owner_id

    def handle(self, chat_id: int, text: str) -> str:
        if not self.is_authorized(chat_id):
            return "Unauthorized."

        parts = (text or "").strip().split()
        if not parts:
            return HELP
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self._status()
        if cmd == "/scan":
            return self._scan()
        if cmd == "/allocate":
            return self._allocate(args)
        if cmd == "/plan":
            return self._plan(args)
        if cmd == "/execute":
            return self._execute(chat_id, args)
        if cmd == "/confirm":
            return self._confirm(chat_id)
        if cmd == "/cancel":
            self._pending.pop(chat_id, None)
            return "Staged trade discarded."
        return f"Unknown command: {cmd}\n\n{HELP}"

    # -- helpers ---------------------------------------------------------- #

    def _status(self) -> str:
        ready, missing = self.exec_config.live_ready()
        mode = self.exec_config.mode
        line = (
            f"mode={mode} | max_stake=${self.exec_config.max_stake:.2f} | "
            f"slippage={self.exec_config.slippage:.2%}"
        )
        if mode == "live" and not ready:
            line += f"\nLIVE requested but missing creds: {missing} (will refuse)."
        elif mode != "live":
            line += "\nDry-run: /confirm will simulate only, never place orders."
        return line

    def _find(self, market_id: str) -> Optional[Opportunity]:
        for op in self.scan_fn():
            if op.market_id == market_id:
                return op
        return None

    def _scan(self) -> str:
        ops = self.scan_fn()
        if not ops:
            return "No arbitrage opportunities right now."
        lines = ["Opportunities (use the id with /plan or /execute):"]
        for op in ops[:15]:
            apr = "instant" if op.annualized_pct is None else f"{op.annualized_pct:.0f}% APR"
            lines.append(
                f"- {op.market_id} | {op.kind} | edge {op.edge_pct:.2f}% "
                f"({apr}) | {op.question[:40]}"
            )
        return "\n".join(lines)

    def _allocate(self, args: list[str]) -> str:
        if not args:
            return "Usage: /allocate <bankroll>"
        try:
            bankroll = float(args[0])
        except ValueError:
            return f"Not a number: {args[0]}"
        summary = allocate_portfolio(
            self.scan_fn(), SizingConfig(bankroll=bankroll)
        )
        return format_portfolio(summary)

    def _plan(self, args: list[str]) -> str:
        if not args:
            return "Usage: /plan <market_id>"
        op = self._find(args[0])
        if op is None:
            return f"No current opportunity with id {args[0]} (try /scan)."
        if op.kind != ARB_BUY_SET:
            return f"{op.kind} is not executable in this version (only {ARB_BUY_SET})."
        try:
            plan = build_order_plan(op, self.exec_config.max_stake, self.exec_config.slippage)
        except Exception as exc:  # noqa: BLE001
            return f"Could not build plan: {exc}"
        from .execution import simulate

        return simulate(plan, self.exec_config)

    def _execute(self, chat_id: int, args: list[str]) -> str:
        if not args:
            return "Usage: /execute <market_id>"
        op = self._find(args[0])
        if op is None:
            return f"No current opportunity with id {args[0]} (try /scan)."
        if op.kind != ARB_BUY_SET:
            return f"{op.kind} is not executable in this version (only {ARB_BUY_SET})."
        try:
            plan = build_order_plan(op, self.exec_config.max_stake, self.exec_config.slippage)
        except Exception as exc:  # noqa: BLE001
            return f"Could not build plan: {exc}"
        self._pending[chat_id] = plan

        from .execution import simulate

        verb = "PLACE THIS LIVE" if self.exec_config.mode == "live" else "simulate (dry-run)"
        return f"{simulate(plan, self.exec_config)}\n\nReply /confirm to {verb}, or /cancel."

    def _confirm(self, chat_id: int) -> str:
        plan = self._pending.pop(chat_id, None)
        if plan is None:
            return "Nothing staged. Use /execute <market_id> first."
        result = self.executor.execute(plan)
        return result.detail
