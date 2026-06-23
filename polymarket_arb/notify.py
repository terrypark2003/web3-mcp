"""One-shot Telegram notifier for scheduled runs (e.g. GitHub Actions).

Unlike ``telegram_bot.py`` (a long-running interactive bot), this is a single
scan-and-send pass with no persistent process: it fits a cron / CI schedule.
It scans, diffs against the previous run's state, and pushes only *new*
risk-free edges to a Telegram chat via the Bot API (a plain HTTPS POST — no
python-telegram-bot dependency).

Env vars:
    TELEGRAM_BOT_TOKEN   from @BotFather (required)
    TELEGRAM_CHAT_ID     your numeric chat id (or TELEGRAM_OWNER_ID) (required)
    NOTIFY_DEMO=1        send a labelled demo message (to test the wiring)
    NOTIFY_INCLUDE_EV=1  also include positive-EV signals (opinion, off by default)
    NOTIFY_MIN_EDGE_PCT  only notify on edges at/above this percent (default 0)
    NOTIFY_STATE_FILE    dedup state path (default notify_state.json)

Dedup is via a small JSON state file; the scheduler persists it between runs
(GitHub Actions cache). A vanished-then-reappeared edge re-fires, matching the
bot's alert semantics.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .webapp import read_only_payload

_STATE_FILE = os.environ.get("NOTIFY_STATE_FILE", "notify_state.json")
_TELEGRAM_API = "https://api.telegram.org"


def _poly_key(op: dict) -> tuple:
    return ("poly", str(op.get("market_id")), str(op.get("kind")))


def _cross_key(op: dict) -> tuple:
    return ("cross", str(op.get("event_id")), str(op.get("yes_venue")),
            str(op.get("no_venue")))


def _ev_key(op: dict) -> tuple:
    return ("ev", str(op.get("market_id")), str(op.get("side")))


def _wc_key(op: dict) -> tuple:
    return ("wc", str(op.get("market_id")), str(op.get("side")))


def compute_notification(
    payload: dict,
    seen_keys,
    include_ev: bool = False,
    min_edge_pct: float = 0.0,
) -> tuple[Optional[str], list]:
    """Return (message_or_None, new_seen_keys) given a payload and prior state.

    ``new_seen_keys`` is reset to whatever is currently live (JSON-friendly
    lists), so disappeared edges re-fire when they come back.
    """
    poly = [o for o in payload.get("polymarket", []) if o.get("edge_pct", 0) >= min_edge_pct]
    cross = [o for o in payload.get("cross_venue", []) if o.get("edge_pct", 0) >= min_edge_pct]
    ev = payload.get("ev", []) if include_ev else []
    world_cup = payload.get("world_cup", [])  # always included when present

    current: dict[tuple, tuple] = {}
    for op in poly:
        current[_poly_key(op)] = ("poly", op)
    for op in cross:
        current[_cross_key(op)] = ("cross", op)
    for op in ev:
        current[_ev_key(op)] = ("ev", op)
    for op in world_cup:
        current[_wc_key(op)] = ("wc", op)

    seen = {tuple(k) for k in seen_keys}
    new_keys = [k for k in current if k not in seen]
    new_seen = [list(k) for k in current]
    if not new_keys:
        return None, new_seen

    new_poly = [current[k][1] for k in new_keys if k[0] == "poly"]
    new_cross = [current[k][1] for k in new_keys if k[0] == "cross"]
    new_ev = [current[k][1] for k in new_keys if k[0] == "ev"]
    new_wc = [current[k][1] for k in new_keys if k[0] == "wc"]
    new_poly.sort(key=lambda o: o.get("edge_pct", 0), reverse=True)
    new_cross.sort(key=lambda o: o.get("total_edge", 0), reverse=True)
    new_ev.sort(key=lambda o: o.get("ev_per_contract", 0), reverse=True)
    new_wc.sort(key=lambda o: o.get("ev_per_contract", 0), reverse=True)

    src = payload.get("meta", {}).get("source", "demo")
    header = "⚽ World Cup value" if new_wc and not (new_poly or new_cross or new_ev) \
        else "\U0001f514 New arbitrage"
    lines = [f"{header} ({src}):"]
    if new_poly:
        lines.append("Polymarket:")
        for o in new_poly:
            apr = "instant" if o.get("annualized_pct") is None else f"{o['annualized_pct']:.0f}% APR"
            lines.append(
                f"- {o.get('question','')[:40]} | {o.get('kind')} | "
                f"edge {o.get('edge_pct',0):.2f}% ({apr}) | ${o.get('total_edge',0):.0f}"
            )
    if new_cross:
        lines.append("Cross-venue:")
        for o in new_cross:
            lines.append(
                f"- {o.get('question','')[:38]} | edge {o.get('edge_pct',0):.2f}% "
                f"(${o.get('total_edge',0):.0f}) | YES@{o.get('yes_venue')} "
                f"{o.get('yes_price',0):.2f} + NO@{o.get('no_venue')} {o.get('no_price',0):.2f}"
            )
    if new_ev:
        lines.append("Positive-EV (NOT risk-free):")
        for o in new_ev:
            lines.append(
                f"- {o.get('question','')[:34]} | {o.get('side')}@{o.get('venue')} "
                f"{o.get('price',0):.2f} vs fair {o.get('fair_prob',0):.2f} | "
                f"EV {o.get('ev_per_contract',0):+.3f}/ct"
            )
    if new_wc:
        lines.append("World Cup value (NOT risk-free — vs bookmaker consensus):")
        for o in new_wc:
            lines.append(
                f"- {o.get('question','')[:40]} | {o.get('side')} @ "
                f"{o.get('price',0):.2f} vs consensus {o.get('fair_prob',0):.2f} | "
                f"EV {o.get('ev_per_contract',0):+.3f}/ct ({o.get('edge_pct',0):.0f}%)"
            )
    return "\n".join(lines), new_seen


def load_state(path: str = _STATE_FILE) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("seen", [])
    except (OSError, ValueError):
        return []


def save_state(seen: list, path: str = _STATE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"seen": seen}, fh)


def _empty_payload(meta: dict) -> dict:
    return {"polymarket": [], "cross_venue": [], "ev": [], "world_cup": [], "meta": meta}


def build_world_cup_payload(demo: bool = False) -> dict:
    """World Cup value bets vs bookmaker consensus, in the notification shape.

    Demo uses the bundled fixture. Live needs ODDS_API_KEY; any failure (no key,
    egress, API change) returns a ``meta.source == "error"`` payload so the
    caller can skip sending rather than alert on bad data.
    """
    from .scanner import ev_to_dict

    try:
        if demo:
            from .demo import load_demo_world_cup

            ops = load_demo_world_cup()
            meta = {"source": "demo"}
        else:
            key = os.environ.get("ODDS_API_KEY")
            if not key:
                return _empty_payload({"source": "error", "error": "ODDS_API_KEY not set"})
            from .client import PolymarketClient
            from .multivenue import scan_world_cup_value_live
            from .odds_api import OddsApiClient

            min_edge = float(os.environ.get("NOTIFY_WC_MIN_EDGE", "0.03") or "0.03")
            ops = scan_world_cup_value_live(
                PolymarketClient(), OddsApiClient(key), min_edge=min_edge
            )
            meta = {"source": "live"}
    except Exception as exc:  # noqa: BLE001 - never alert on a failed scan
        return _empty_payload({"source": "error", "error": str(exc)[:200]})

    payload = _empty_payload(meta)
    payload["world_cup"] = [ev_to_dict(o) for o in ops]
    return payload


def maybe_gemini_note(payload: dict) -> Optional[str]:
    """One cautious Gemini line about the current signals, or None.

    Gated on GEMINI_ENRICH + GEMINI_API_KEY. Never raises — enrichment must not
    break an alert, and Gemini is commentary only, not the value judgement.
    """
    if os.environ.get("GEMINI_ENRICH", "").lower() not in ("1", "true", "yes"):
        return None
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from .gemini import NOTE_SYSTEM, GeminiClient, build_signal_context

        context = build_signal_context(payload)
        note = GeminiClient(key).generate(
            f"Data:\n{context}\n\nWrite one short cautious context line.", NOTE_SYSTEM
        )
        return note.strip() or None
    except Exception:  # noqa: BLE001 - enrichment is best-effort
        return None


def send_telegram(token: str, chat_id: str, text: str) -> None:  # pragma: no cover - network
    import requests

    resp = requests.post(
        f"{_TELEGRAM_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    resp.raise_for_status()


def main() -> int:  # pragma: no cover - orchestration, exercised via the workflow
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_OWNER_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")
        return 1

    demo = os.environ.get("NOTIFY_DEMO", "").lower() in ("1", "true", "yes")
    mode = os.environ.get("NOTIFY_MODE", "arb").lower()

    if mode == "world_cup":
        payload = build_world_cup_payload(demo=demo)
        if payload.get("meta", {}).get("source") == "error":
            print(f"world cup scan unavailable ({payload['meta'].get('error')}); "
                  "no notification sent.")
            return 0
    else:
        payload = read_only_payload(live=not demo)
        # Never send demo data as if it were a real live alert.
        if not demo and payload.get("meta", {}).get("source") != "live":
            err = payload.get("meta", {}).get("live_error", "unknown")
            print(f"live scan unavailable ({err}); no notification sent.")
            return 0

    include_ev = os.environ.get("NOTIFY_INCLUDE_EV", "").lower() in ("1", "true", "yes")
    min_edge = float(os.environ.get("NOTIFY_MIN_EDGE_PCT", "0") or "0")

    text, new_seen = compute_notification(
        payload, load_state(), include_ev=include_ev, min_edge_pct=min_edge
    )
    save_state(new_seen)

    if text is None:
        print("No new opportunities since last run.")
        return 0
    note = maybe_gemini_note(payload)
    if note:
        text += f"\n\n\U0001f916 Gemini: {note}"
    if demo:
        text = "[DEMO TEST — not real data]\n" + text
    send_telegram(token, chat_id, text)
    print("Notification sent.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
