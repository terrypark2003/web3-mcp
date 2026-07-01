"""Win/loss settlement watcher — tells you when a held position resolves.

Pure and testable: ``check_settlements`` takes the raw Data API position rows
plus a small persisted state dict and returns (messages, new_state). The
Telegram wiring polls this periodically and pushes any messages to the owner.

HOW A SETTLEMENT IS DETECTED
-----------------------------
Polymarket's ``/positions`` response gives each held outcome token's
``curPrice`` (0..1) and ``redeemable`` flag. Once a market resolves, the
winning outcome's token snaps to ~1 (and becomes redeemable); the losing
outcome's token snaps to ~0. This module tracks each position by a stable key
(conditionId + outcome) across polls:

- First time a key is ever seen already-resolved (curPrice near 0 or 1): this
  is "old news" from before the bot started tracking it — recorded silently,
  no message (avoids spamming every already-settled position on first run).
- A key seen OPEN (curPrice strictly between the thresholds) and later
  resolved: fires WIN or LOSS.
- A previously-open key that disappears entirely from the response (fully
  redeemed/cleared from the wallet) is treated as WIN — Polymarket clears
  redeemed winning tokens from the holder's balance; losing tokens are
  worthless but typically still listed at curPrice 0.

This inference is Polymarket API behavior that isn't fully documented, so
treat the first live settlement as your validation, same as this repo's other
live-only paths.
"""

from __future__ import annotations

import json
from typing import Optional

WIN = "WIN"
LOSS = "LOSS"
SETTLED = "SETTLED"  # disappeared from the wallet; assumed redeemed (a win)

_RESOLVED_HI = 0.99
_RESOLVED_LO = 0.01

_STATE_FILE = "settlement_state.json"


def position_key(p: dict) -> str:
    cond = p.get("conditionId") or p.get("asset") or p.get("market") or ""
    outcome = p.get("outcome") or ""
    return f"{cond}:{outcome}"


def check_settlements(
    raw_positions: list[dict],
    state: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """Diff ``raw_positions`` against ``state`` and return (events, new_state).

    ``state`` shape: ``{"open": {key: {...}}, "notified": [key, ...]}``.
    Each event: ``{"key", "title", "outcome", "kind", "value", "pnl"}``.
    """
    state = state or {}
    prev_open: dict = dict(state.get("open") or {})
    notified: set = set(state.get("notified") or [])

    events: list[dict] = []
    new_open: dict = {}
    seen_keys: set = set()

    for p in raw_positions:
        key = position_key(p)
        seen_keys.add(key)
        cur_price = _num(p.get("curPrice"))
        resolved = cur_price >= _RESOLVED_HI or cur_price <= _RESOLVED_LO
        info = {
            "title": p.get("title") or p.get("slug") or "?",
            "outcome": p.get("outcome") or "",
            "value": _num(p.get("currentValue")),
            "pnl": _num(p.get("cashPnl")),
        }

        if not resolved:
            new_open[key] = info
            continue

        if key in notified:
            continue  # already told the user about this one

        if key not in prev_open:
            # Never tracked as open before (cold start / new position that was
            # already settled by the time we first saw it) — baseline silently.
            notified.add(key)
            continue

        kind = WIN if (cur_price >= _RESOLVED_HI or p.get("redeemable")) else LOSS
        events.append({"key": key, "kind": kind, **info})
        notified.add(key)

    # Previously-open positions that vanished entirely from the response —
    # Polymarket clears a redeemed winner from the wallet, so treat as a win.
    for key, info in prev_open.items():
        if key in seen_keys or key in notified:
            continue
        events.append({"key": key, "kind": SETTLED, **info})
        notified.add(key)

    return events, {"open": new_open, "notified": sorted(notified)}


def format_settlement_message(event: dict) -> str:
    title = str(event["title"])[:50]
    outcome = event["outcome"]
    pnl = event["pnl"]
    sign = "+" if pnl >= 0 else "-"
    if event["kind"] == WIN:
        return (f"🎉 적중! '{title}' — {outcome}\n"
                f"정산 완료, 수령액 ${event['value']:,.2f} ({sign}${abs(pnl):,.2f})")
    if event["kind"] == LOSS:
        return (f"💔 낙첨 — '{title}' — {outcome}\n"
                f"빗나갔습니다 ({sign}${abs(pnl):,.2f})")
    return (f"✅ 정산됨 — '{title}' — {outcome}\n"
            f"지갑에서 정리됨 (대부분 적중/수령 완료 신호, {sign}${abs(pnl):,.2f})")


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_state(path: str = _STATE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: str = _STATE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
