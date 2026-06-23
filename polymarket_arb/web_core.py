"""Web dashboard logic — pure and testable, no web framework dependency.

Mirrors ``bot_core.ArbBot`` but for an HTTP dashboard: it takes plain Python
arguments and returns JSON-serializable dicts, so the FastAPI shell in
``webapp.py`` stays a thin adapter and the whole thing is unit-testable without
a server or a network.

SAFETY MODEL (this dashboard can place REAL orders)
---------------------------------------------------
Same gates as the Telegram bot, plus token auth because a browser is exposed:

* **Token auth on every call.** ``check_auth`` uses a constant-time compare.
  If no ``auth_token`` is configured the service denies everything — a dashboard
  that can trade must not run unauthenticated.
* **Dry-run by default.** ``confirm`` only places orders when
  ``EXECUTION_MODE=live`` AND credentials are present (enforced in
  ``execution.py``). Otherwise it simulates and places nothing.
* **Two-step stage -> confirm.** ``stage`` builds and stores the exact order
  plan and returns it for the operator to see; ``confirm`` executes a
  previously staged plan by id. Nothing is placed in one click.
* **Only BUY_SET is executable** (same as the bot / execution layer).
"""

from __future__ import annotations

import hmac
import uuid
from typing import Callable, Optional

from .execution import (
    ExecutionConfig,
    OrderPlan,
    PolymarketExecutor,
    build_order_plan,
    simulate,
)
from .models import ARB_BUY_SET, Opportunity
from .scanner import cross_to_dict, ev_to_dict, opportunity_to_dict


class AuthError(Exception):
    """Raised when a request is missing or fails token authentication."""


class DashboardError(Exception):
    """Raised for a bad request (unknown id, non-executable kind, etc.)."""


def plan_to_dict(plan: OrderPlan) -> dict:
    return {
        "market_id": plan.market_id,
        "question": plan.question,
        "kind": plan.kind,
        "sets": round(plan.sets, 4),
        "total_cost": round(plan.total_cost, 2),
        "expected_payoff": round(plan.expected_payoff, 2),
        "expected_profit": round(plan.expected_profit, 2),
        "legs": [
            {
                "token_id": leg.token_id,
                "outcome": leg.outcome,
                "side": leg.side,
                "price": round(leg.price, 4),
                "size": round(leg.size, 2),
            }
            for leg in plan.legs
        ],
    }


class DashboardService:
    def __init__(
        self,
        scan_fn: Callable[[], list[Opportunity]],
        executor: PolymarketExecutor,
        exec_config: ExecutionConfig,
        cross_scan_fn: Optional[Callable[[], list]] = None,
        ev_scan_fn: Optional[Callable[[], list]] = None,
        auth_token: Optional[str] = None,
    ) -> None:
        self.scan_fn = scan_fn
        self.executor = executor
        self.exec_config = exec_config
        self.cross_scan_fn = cross_scan_fn
        self.ev_scan_fn = ev_scan_fn
        self.auth_token = auth_token or None
        self._staged: dict[str, OrderPlan] = {}

    # -- auth -------------------------------------------------------------- #

    def check_auth(self, token: Optional[str]) -> bool:
        """Constant-time token check. Denies everything if no token is set."""
        if not self.auth_token:
            return False
        return hmac.compare_digest(str(token or ""), self.auth_token)

    def require_auth(self, token: Optional[str]) -> None:
        if not self.check_auth(token):
            raise AuthError("invalid or missing dashboard token")

    # -- read -------------------------------------------------------------- #

    def status(self) -> dict:
        ready, missing = self.exec_config.live_ready()
        return {
            "mode": self.exec_config.mode,
            "max_stake": self.exec_config.max_stake,
            "slippage": self.exec_config.slippage,
            "live_ready": ready,
            "missing_creds": missing,
            "executable_kind": ARB_BUY_SET,
            "cross_enabled": self.cross_scan_fn is not None,
            "ev_enabled": self.ev_scan_fn is not None,
        }

    def opportunities(self) -> dict:
        cross = list(self.cross_scan_fn()) if self.cross_scan_fn else []
        ev = list(self.ev_scan_fn()) if self.ev_scan_fn else []
        return {
            "polymarket": [opportunity_to_dict(o) for o in self.scan_fn()],
            "cross_venue": [cross_to_dict(o) for o in cross],
            "ev": [ev_to_dict(o) for o in ev],
        }

    # -- execute (stage -> confirm) --------------------------------------- #

    def _find(self, market_id: str) -> Optional[Opportunity]:
        for op in self.scan_fn():
            if op.market_id == market_id:
                return op
        return None

    def stage(self, market_id: str, max_stake: Optional[float] = None) -> dict:
        op = self._find(market_id)
        if op is None:
            raise DashboardError(f"no current opportunity with id {market_id}")
        if op.kind != ARB_BUY_SET:
            raise DashboardError(
                f"{op.kind} is not executable (only {ARB_BUY_SET})"
            )
        stake = self.exec_config.max_stake if max_stake is None else float(max_stake)
        if stake <= 0:
            raise DashboardError("max_stake must be positive")
        try:
            plan = build_order_plan(op, stake, self.exec_config.slippage)
        except Exception as exc:  # noqa: BLE001 - surface as a clean 400
            raise DashboardError(f"could not build plan: {exc}") from exc

        stage_id = uuid.uuid4().hex
        self._staged[stage_id] = plan
        return {
            "stage_id": stage_id,
            "mode": self.exec_config.mode,
            "will_place_live": self.exec_config.mode == "live",
            "plan": plan_to_dict(plan),
            "preview": simulate(plan, self.exec_config),
        }

    def confirm(self, stage_id: str) -> dict:
        plan = self._staged.pop(stage_id, None)
        if plan is None:
            raise DashboardError("nothing staged with that id (re-stage first)")
        result = self.executor.execute(plan)
        return {
            "placed": result.placed,
            "dry_run": result.dry_run,
            "detail": result.detail,
            "filled_legs": result.filled_legs,
        }

    def cancel(self, stage_id: str) -> dict:
        existed = self._staged.pop(stage_id, None) is not None
        return {"cancelled": existed}
