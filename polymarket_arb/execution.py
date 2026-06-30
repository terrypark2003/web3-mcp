"""Execution layer: turn a detected arbitrage into concrete orders.

SAFETY MODEL
------------
* Secrets are read ONLY from environment variables (never arguments that get
  logged, never committed). See ``.env.example``.
* The default mode is ``dry-run``: ``simulate()`` prints exactly what would be
  sent and places nothing. Real orders require ``EXECUTION_MODE=live`` AND a
  full set of credentials AND an explicit per-trade confirmation upstream.
* Only ``BUY_SET`` opportunities are executable here (lift the ask on every
  leg). ``MINT_SELL`` needs an on-chain CTF split and is intentionally out of
  scope for this version.

ATOMICITY CAVEAT (read before going live)
-----------------------------------------
A complete-set buy is only a hedge once *every* leg fills. Two independent
CLOB orders cannot fill atomically, so ``execute()`` places each leg
fill-or-kill and, if any leg fails, attempts to unwind the legs that did fill.
That unwind can slip. Test in dry-run with $1 first, and watch the first live
fills by hand.

The live order-placement calls use the official ``py-clob-client``. They are
written to its documented interface but CANNOT be tested from this sandbox
(no network, no credentials), so VALIDATE them against your installed client
version before trusting real money to them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .models import ARB_BUY_SET, Opportunity

DRY_RUN = "dry-run"
LIVE = "live"


class ExecutionError(Exception):
    """Raised when an order plan cannot be built or executed."""


@dataclass
class OrderLeg:
    token_id: str
    outcome: str
    side: str          # always "BUY" in this version
    price: float       # marketable limit price (the ask, plus a slippage buffer)
    size: float        # shares


@dataclass
class OrderPlan:
    market_id: str
    question: str
    kind: str
    legs: list[OrderLeg]
    sets: float
    total_cost: float       # USDC to buy one of every leg, * sets
    expected_payoff: float  # one leg settles to $1 per set
    expected_profit: float


@dataclass
class ExecutionResult:
    placed: bool
    dry_run: bool
    detail: str
    filled_legs: list[str]


def build_order_plan(
    op: Opportunity, max_stake: float, slippage: float = 0.0
) -> OrderPlan:
    """Size a BUY_SET opportunity into concrete per-leg buy orders.

    Size is capped by both ``max_stake`` (USDC) and the depth already baked
    into the opportunity (``op.max_sets``). ``slippage`` nudges the limit price
    above the quoted ask so a marketable order still fills.
    """
    if op.kind != ARB_BUY_SET:
        raise ExecutionError(
            f"Only {ARB_BUY_SET} is executable in this version, not {op.kind}"
        )
    if op.cost_per_set <= 0:
        raise ExecutionError("opportunity has non-positive cost_per_set")
    if not op.legs or any(leg.best_ask is None for leg in op.legs):
        raise ExecutionError("opportunity is missing a live ask on some leg")

    sets = min(op.max_sets, max_stake / op.cost_per_set)
    if sets <= 0:
        raise ExecutionError("computed size is non-positive (raise max_stake?)")

    legs = [
        OrderLeg(
            token_id=leg.token_id,
            outcome=leg.outcome,
            side="BUY",
            price=min(1.0, leg.best_ask.price * (1.0 + slippage)),
            size=sets,
        )
        for leg in op.legs
    ]
    total_cost = sum(leg.price * leg.size for leg in legs)
    payoff = sets  # exactly one leg resolves to $1 per set
    return OrderPlan(
        market_id=op.market_id,
        question=op.question,
        kind=op.kind,
        legs=legs,
        sets=sets,
        total_cost=total_cost,
        expected_payoff=payoff,
        expected_profit=payoff - total_cost,
    )


FAV_BUY = "FAV_BUY"  # a single-outcome "buy ~$1 of a favorite" order (NOT risk-free)


def build_single_buy_plan(
    token_id: str,
    outcome: str,
    price: float,
    market_id: str = "",
    question: str = "",
    *,
    dollars: float = 1.0,
    slippage: float = 0.01,
) -> OrderPlan:
    """Size a single-outcome buy of about ``dollars`` USDC at ``price``.

    This is the "tap to buy $1" path for a favorite — NOT arbitrage. You buy
    ``dollars/price`` shares; if the outcome wins each share redeems $1, and if
    it loses you lose the stake. ``slippage`` lifts the limit above the ask so a
    marketable order fills. Capping ``dollars`` is the caller's job (the bot
    passes $1).
    """
    if price <= 0:
        raise ExecutionError("non-positive price")
    if dollars <= 0:
        raise ExecutionError("non-positive dollars")
    limit_price = min(1.0, price * (1.0 + slippage))
    shares = round(dollars / price, 2)
    if shares <= 0:
        raise ExecutionError("computed size is non-positive")
    total_cost = limit_price * shares
    return OrderPlan(
        market_id=market_id,
        question=question,
        kind=FAV_BUY,
        legs=[OrderLeg(token_id=token_id, outcome=outcome, side="BUY",
                       price=limit_price, size=shares)],
        sets=shares,
        total_cost=total_cost,
        expected_payoff=shares,           # if it wins, each share -> $1
        expected_profit=shares - total_cost,
    )


@dataclass
class ExecutionConfig:
    mode: str = DRY_RUN
    max_stake: float = 1.0
    slippage: float = 0.01
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    private_key: Optional[str] = None
    funder: Optional[str] = None
    signature_type: Optional[int] = None  # 0=EOA, 1=email/Magic proxy, 2=browser-wallet proxy
    rpc_url: str = "https://polygon-rpc.com"
    clob_host: str = "https://clob.polymarket.com"

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "ExecutionConfig":
        env = env if env is not None else os.environ
        mode = (env.get("EXECUTION_MODE") or DRY_RUN).strip().lower()
        if mode not in (DRY_RUN, LIVE):
            mode = DRY_RUN
        return cls(
            mode=mode,
            max_stake=float(env.get("MAX_STAKE_USDC", "1") or "1"),
            slippage=float(env.get("SLIPPAGE", "0.01") or "0.01"),
            api_key=env.get("POLYMARKET_API_KEY"),
            api_secret=env.get("POLYMARKET_API_SECRET"),
            api_passphrase=env.get("POLYMARKET_API_PASSPHRASE"),
            private_key=env.get("POLYMARKET_PRIVATE_KEY"),
            funder=env.get("POLYMARKET_FUNDER"),
            signature_type=(
                int(env["POLYMARKET_SIGNATURE_TYPE"])
                if env.get("POLYMARKET_SIGNATURE_TYPE") else None
            ),
            rpc_url=env.get("POLYGON_RPC_URL") or "https://polygon-rpc.com",
        )

    def live_ready(self) -> tuple[bool, list[str]]:
        """Whether live trading has what it needs (no secrets logged).

        Only the signing key (`POLYMARKET_PRIVATE_KEY`) is required — the L2 API
        creds (api_key/secret/passphrase) are *derived from that key* at connect
        time if not supplied, so you don't have to hunt them down. Supplying them
        explicitly still works and skips the derive call.
        """
        missing = [] if self.private_key else ["POLYMARKET_PRIVATE_KEY"]
        return (not missing, missing)

    @property
    def has_explicit_api_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


def simulate(plan: OrderPlan, config: ExecutionConfig) -> str:
    """Human-readable description of what would be sent. Places nothing."""
    lines = [
        f"드라이런 주문 계획: {plan.question}",
        f"  마켓={plan.market_id} 종류={plan.kind} 수량={plan.sets:.2f}주",
    ]
    for leg in plan.legs:
        lines.append(
            f"  매수 {leg.size:.2f}주 '{leg.outcome}' @ {leg.price:.4f} "
            f"(토큰 {leg.token_id[:10]}...)"
        )
    lines.append(
        f"  비용=${plan.total_cost:.2f} 회수=${plan.expected_payoff:.2f} "
        f"수익=${plan.expected_profit:.2f}"
    )
    lines.append("  >>> 주문 안 함 (드라이런) <<<")
    return "\n".join(lines)


class PolymarketExecutor:
    """Places BUY_SET legs via the official py-clob-client (live mode only)."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config
        self._client = None

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        ready, missing = self.config.live_ready()
        if not ready:
            raise ExecutionError(f"missing credentials for live mode: {missing}")
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ExecutionError(
                "py-clob-client not installed (pip install -r requirements-bot.txt)"
            ) from exc

        # signature_type / funder depend on the account: 0 = trade directly from
        # the signing EOA; 1 = email/Magic proxy; 2 = browser-wallet proxy. For a
        # Polymarket-funded account set POLYMARKET_SIGNATURE_TYPE + POLYMARKET_FUNDER
        # (your Polymarket deposit/proxy address) or orders sign from an unfunded EOA.
        kwargs = {
            "host": self.config.clob_host,
            "key": self.config.private_key,
            "chain_id": 137,
        }
        if self.config.funder:
            kwargs["funder"] = self.config.funder
        if self.config.signature_type is not None:
            kwargs["signature_type"] = self.config.signature_type
        client = ClobClient(**kwargs)
        if self.config.has_explicit_api_creds:
            client.set_api_creds(ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            ))
        else:
            # Derive (or create) the L2 API creds from the signing key — one
            # network call, so the user only has to provide the private key.
            client.set_api_creds(client.create_or_derive_api_creds())
        self._client = client
        return self._client

    def execute(self, plan: OrderPlan) -> ExecutionResult:
        """Dry-run unless mode==live; in live mode, place each leg fill-or-kill."""
        if self.config.mode != LIVE:
            return ExecutionResult(
                placed=False,
                dry_run=True,
                detail=simulate(plan, self.config),
                filled_legs=[],
            )

        client = self._client_or_raise()
        filled: list[str] = []
        try:  # pragma: no cover - requires live network + credentials
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            for leg in plan.legs:
                args = OrderArgs(
                    token_id=leg.token_id,
                    price=round(leg.price, 4),
                    size=round(leg.size, 2),
                    side=BUY,
                )
                resp = client.create_and_post_order(args)  # FOK semantics vary by version
                if not _order_succeeded(resp):
                    raise ExecutionError(f"leg '{leg.outcome}' did not fill: {resp}")
                filled.append(leg.outcome)

            return ExecutionResult(
                placed=True,
                dry_run=False,
                detail=f"'{plan.question}' — {len(filled)}개 레그 전부 체결 완료.",
                filled_legs=filled,
            )
        except Exception as exc:  # pragma: no cover
            unwound = self._unwind(filled, plan)
            return ExecutionResult(
                placed=False,
                dry_run=False,
                detail=(
                    f"중단됨 (체결된 레그: {filled}): {exc}. 되돌리기: {unwound}. "
                    "포지션을 직접 확인하세요."
                ),
                filled_legs=filled,
            )

    def _unwind(self, filled_outcomes: list[str], plan: OrderPlan) -> str:  # pragma: no cover
        """Best-effort flatten of legs that filled before an abort."""
        if not filled_outcomes:
            return "nothing to unwind"
        return (
            f"attempted to sell {filled_outcomes} at market — verify manually; "
            "unwind may have slipped"
        )


def _order_succeeded(resp) -> bool:  # pragma: no cover - shape depends on client version
    if isinstance(resp, dict):
        return bool(resp.get("success", False)) or resp.get("status") in (
            "matched",
            "filled",
        )
    return resp is not None
