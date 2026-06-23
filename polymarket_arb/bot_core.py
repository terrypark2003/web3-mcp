"""Telegram bot command logic — pure and testable.

This module has no Telegram or network dependency: it takes a chat id and a
command string and returns a reply string. The Telegram wiring in
``telegram_bot.py`` is a thin async shell over ``ArbBot.handle``. Opportunity
discovery is injected as ``scan_fn`` so it can be stubbed in tests (and so the
real bot supplies the live scanner).
"""

from __future__ import annotations

from typing import Callable, Optional

from .crossvenue import CrossVenueOpportunity
from .detect import FeeModel
from .ev import EVOpportunity
from .execution import (
    ExecutionConfig,
    OrderPlan,
    PolymarketExecutor,
    build_order_plan,
)
from .models import ARB_BUY_SET, Opportunity
from .portfolio import SizingConfig, allocate_portfolio, format_portfolio
from .scanner import cross_to_dict, ev_to_dict, opportunity_to_dict

HELP = (
    "Prediction-market arbitrage bot (owner-only)\n"
    "/scan - find Polymarket complete-set arbitrage\n"
    "/cross - cross-venue arbitrage (Kalshi vs Polymarket)\n"
    "/ev - positive-EV signals vs fair value (NOT risk-free)\n"
    "/ask <question> - ask Gemini about the current signals (plain-language)\n"
    "/allocate <bankroll> - size bets across the edges\n"
    "/plan <market_id> - show the order plan for one opportunity\n"
    "/execute <market_id> - stage a trade, then /confirm to place it\n"
    "/confirm - execute the staged trade (live only; dry-run otherwise)\n"
    "/cancel - discard the staged trade\n"
    "/alerts <on|off|status> - proactive push when new arbs appear\n"
    "/status - show mode, max stake, and live-readiness"
)


def _alert_key(op: Opportunity) -> tuple:
    return ("poly", op.market_id, op.kind)


def _cross_key(op: CrossVenueOpportunity) -> tuple:
    return ("cross", op.event_id, op.yes_venue, op.no_venue)


def format_poly_lines(ops: list[Opportunity]) -> list[str]:
    out = []
    for op in ops:
        apr = "instant" if op.annualized_pct is None else f"{op.annualized_pct:.0f}% APR"
        out.append(
            f"- {op.market_id} | {op.kind} | edge {op.edge_pct:.2f}% "
            f"({apr}) | {op.question[:40]}"
        )
    return out


def format_alert(ops: list[Opportunity]) -> str:
    lines = ["\U0001f514 New arbitrage:"]
    lines.extend(format_poly_lines(ops))
    lines.append("Use /execute <id> to stage (dry-run unless live).")
    return "\n".join(lines)


def format_cross_lines(ops: list[CrossVenueOpportunity]) -> list[str]:
    out = []
    for op in ops:
        out.append(
            f"- {op.question[:38]} | edge {op.edge_pct:.2f}% (${op.total_edge:.0f}) "
            f"| BUY YES@{op.yes_venue} {op.yes_price:.2f} + NO@{op.no_venue} "
            f"{op.no_price:.2f}"
        )
    return out


def format_ev_lines(ops: list[EVOpportunity]) -> list[str]:
    out = []
    for op in ops:
        out.append(
            f"- {op.question[:34]} | {op.side}@{op.venue} {op.price:.2f} "
            f"vs fair {op.fair_prob:.2f} | EV {op.ev_per_contract:+.3f}/ct "
            f"({op.edge_pct:.0f}%)"
        )
    return out


class ArbBot:
    def __init__(
        self,
        owner_id: int,
        scan_fn: Callable[[], list[Opportunity]],
        executor: PolymarketExecutor,
        exec_config: ExecutionConfig,
        fee_model: Optional[FeeModel] = None,
        min_alert_edge_pct: float = 0.0,
        alerts_enabled: bool = True,
        cross_scan_fn: Optional[Callable[[], list[CrossVenueOpportunity]]] = None,
        ev_scan_fn: Optional[Callable[[], list[EVOpportunity]]] = None,
        signal_channel_id: Optional[int] = None,
        wc_scan_fn: Optional[Callable[[], list[EVOpportunity]]] = None,
        gemini_generate: Optional[Callable[[str, Optional[str]], str]] = None,
    ) -> None:
        self.owner_id = owner_id
        self.scan_fn = scan_fn
        self.executor = executor
        self.exec_config = exec_config
        self.fee_model = fee_model or FeeModel()
        self.min_alert_edge_pct = min_alert_edge_pct
        self.alerts_enabled = alerts_enabled
        self.cross_scan_fn = cross_scan_fn
        self.ev_scan_fn = ev_scan_fn
        self.signal_channel_id = signal_channel_id
        self.wc_scan_fn = wc_scan_fn
        self.gemini_generate = gemini_generate
        self._pending: dict[int, OrderPlan] = {}
        self._alert_seen: set = set()
        self._broadcast_seen: set = set()

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
        if cmd == "/cross":
            return self._cross()
        if cmd == "/ev":
            return self._ev()
        if cmd == "/ask":
            return self._ask(args)
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
        if cmd == "/alerts":
            return self._alerts(args)
        return f"Unknown command: {cmd}\n\n{HELP}"

    def poll_alerts(self) -> Optional[str]:
        """Scan and return a message for opportunities not seen on the last poll.

        Returns None when alerts are off or nothing new cleared the threshold.
        Disappeared-then-reappeared opportunities re-fire (the seen-set is reset
        to whatever is currently live each poll).
        """
        if not self.alerts_enabled:
            return None
        ops = [
            op for op in self.scan_fn() if op.edge_pct >= self.min_alert_edge_pct
        ]
        current = {_alert_key(op): op for op in ops}
        new_keys = set(current) - self._alert_seen
        self._alert_seen = set(current)
        if not new_keys:
            return None
        new_ops = [current[k] for k in current if k in new_keys]
        new_ops.sort(key=lambda o: o.edge_pct, reverse=True)
        return format_alert(new_ops)

    def _cross(self) -> str:
        if self.cross_scan_fn is None:
            return "Cross-venue scanning is not configured on this bot."
        ops = self.cross_scan_fn()
        if not ops:
            return "No cross-venue arbitrage right now."
        lines = ["\U0001f501 Cross-venue arbitrage (risk-free only if both venues "
                 "resolve identically):"]
        lines.extend(format_cross_lines(ops))
        return "\n".join(lines)

    def _ev(self) -> str:
        if self.ev_scan_fn is None:
            return "EV scanning is not configured on this bot."
        ops = self.ev_scan_fn()
        if not ops:
            return "No positive-EV signals right now."
        lines = ["⚠️ Positive-EV signals (NOT risk-free — opinion vs "
                 "fair value; size with care):"]
        lines.extend(format_ev_lines(ops))
        return "\n".join(lines)

    def _gather_payload(self) -> dict:
        """Serialize all currently-available signals for Gemini context."""
        cross = list(self.cross_scan_fn()) if self.cross_scan_fn else []
        ev = list(self.ev_scan_fn()) if self.ev_scan_fn else []
        wc = list(self.wc_scan_fn()) if self.wc_scan_fn else []
        return {
            "polymarket": [opportunity_to_dict(o) for o in self.scan_fn()],
            "cross_venue": [cross_to_dict(o) for o in cross],
            "ev": [ev_to_dict(o) for o in ev],
            "world_cup": [ev_to_dict(o) for o in wc],
        }

    def _ask(self, args: list[str]) -> str:
        if self.gemini_generate is None:
            return "Gemini is not configured (set GEMINI_API_KEY)."
        question = " ".join(args).strip()
        if not question:
            return "Usage: /ask <question>"
        from .gemini import ASK_SYSTEM, build_signal_context

        context = build_signal_context(self._gather_payload())
        user = f"Current data:\n{context}\n\nQuestion: {question}"
        try:
            return self.gemini_generate(user, ASK_SYSTEM)
        except Exception as exc:  # noqa: BLE001 - surface API errors to the chat
            return f"Gemini error: {exc}"

    def poll_broadcast(self) -> Optional[str]:
        """Aggregate newly-appeared risk-free arbs for the signals channel.

        Broadcasts Polymarket structural arbs and cross-venue arbs (the
        guaranteed edges), deduped against the previous broadcast. EV is
        intentionally excluded from the auto-feed — it's an opinion, available
        on demand via /ev. Returns None when nothing new cleared.
        """
        poly_ops = [
            op for op in self.scan_fn() if op.edge_pct >= self.min_alert_edge_pct
        ]
        cross_ops = list(self.cross_scan_fn()) if self.cross_scan_fn else []

        current: dict[tuple, object] = {}
        for op in poly_ops:
            current[_alert_key(op)] = op
        for op in cross_ops:
            current[_cross_key(op)] = op

        new_keys = set(current) - self._broadcast_seen
        self._broadcast_seen = set(current)
        if not new_keys:
            return None

        new_poly = [current[k] for k in new_keys if k[0] == "poly"]
        new_cross = [current[k] for k in new_keys if k[0] == "cross"]
        new_poly.sort(key=lambda o: o.edge_pct, reverse=True)
        new_cross.sort(key=lambda o: o.total_edge, reverse=True)

        lines = ["\U0001f4e2 New risk-free edges:"]
        if new_poly:
            lines.append("Polymarket:")
            lines.extend(format_poly_lines(new_poly))
        if new_cross:
            lines.append("Cross-venue:")
            lines.extend(format_cross_lines(new_cross))
        return "\n".join(lines)

    def _alerts(self, args: list[str]) -> str:
        action = (args[0].lower() if args else "status")
        if action == "on":
            self.alerts_enabled = True
            return "Alerts ON."
        if action == "off":
            self.alerts_enabled = False
            return "Alerts OFF."
        state = "ON" if self.alerts_enabled else "OFF"
        return f"Alerts {state} | min edge {self.min_alert_edge_pct:.2f}%"

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
