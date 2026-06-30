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
import math
import os
from datetime import datetime, timezone
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


def _fav_key(op: dict) -> tuple:
    return ("fav", str(op.get("market_id")), str(op.get("outcome")))


def _usd(value) -> str:
    """Human dollar amount that never collapses a real edge to ``$0``.

    The old ``${x:.0f}`` rounded a $0.32 profit to ``$0`` — which reads as "no
    money here". Show cents for small amounts, whole dollars for large ones.
    """
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "$?"
    if abs(x) < 0.01:
        return "<$0.01"
    if abs(x) < 1000:
        return f"${x:,.2f}"
    return f"${x:,.0f}"


def _link_line(op: dict) -> Optional[str]:
    """A tappable market link line, or None when the URL is unknown."""
    url = op.get("url")
    return f"  \U0001f517 {url}" if url else None


def _resolution_eta(end_date, now: Optional[datetime] = None) -> Optional[str]:
    """Plain-Korean time until resolution (when capital unlocks), or None.

    Held positions (BUY_SET / cross-venue / value bets) lock capital until the
    market resolves, so the holding period matters as much as the edge. Returns
    a coarse Korean string in days ("정산까지 약 191일"); None if the date is
    missing or unparseable. Coarse on purpose — the exact minute is noise here.
    """
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    secs = (dt - now).total_seconds()
    if secs <= 0:
        return "정산 시점 지남"
    days = secs / 86400
    if days >= 1:
        return f"정산까지 약 {round(days)}일"
    hours = secs / 3600
    if hours >= 1:
        return f"정산까지 약 {round(hours)}시간"
    return f"정산까지 약 {max(1, round(secs / 60))}분"


def _days_until(end_date, now: Optional[datetime] = None) -> Optional[float]:
    """Days from ``now`` until ``end_date`` (negative if past); None if unknown."""
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 86400


def _resolves_soon(
    op: dict, max_days: Optional[float], now: Optional[datetime] = None
) -> bool:
    """Keep an op only if it settles within ``max_days`` (or is instant).

    ``MINT_SELL`` pays back the moment you sell, so it never waits on resolution
    and always passes. Held positions (BUY_SET / cross / EV / World Cup) pass
    only when a known resolution date falls inside the window — an unknown date
    is dropped, since "resolves within N days" can't be promised without one.
    """
    if max_days is None:
        return True
    if op.get("kind") == "MINT_SELL":
        return True
    d = _days_until(op.get("end_date"), now)
    return d is not None and 0 <= d <= max_days


def _cents(price) -> str:
    """Polymarket-style cents for a 0–1 share price (matches the buy buttons)."""
    try:
        return f"{float(price) * 100:.1f}¢"
    except (TypeError, ValueError):
        return "?"


def _buy_list_lines(op: dict) -> list:
    """High-visibility 'buy exactly these outcomes' checklist for a BUY_SET arb.

    A complete-set BUY_SET means buying one share of *every* leg. For a
    multi-outcome (negative-risk) market that's the 'Yes' of each candidate; for
    a binary market it's both Yes and No. Spelling the legs out — with the same
    cents Polymarket shows on its buy buttons — turns the vague 'buy all
    outcomes' into a checklist the user can execute without opening the market.
    """
    if op.get("kind") != "BUY_SET":
        return []
    legs = op.get("legs") or []
    if not legs:
        return []
    out = ["  👉 이 결과들을 같은 수량으로 매수:"]
    for leg in legs:
        name = str(leg.get("outcome", "")).strip()
        # Binary legs are literally "Yes"/"No"; candidate buckets need the side.
        label = name if name.lower() in ("yes", "no") else f"{name} → Yes"
        out.append(f"     • {label[:44]}  {_cents(leg.get('ask'))}")
    return out


def _realism_line(op: dict) -> Optional[str]:
    """One at-a-glance realism line for a risk-free op, when a score is present.

    The score (see ``realism.py``) folds depth over the $1 floor, capital
    lockup, leg count and slippage buffer into 0-100. A 🟢/🟡/🔴 marker makes
    "is this actually executable?" readable without parsing the numbers — the
    exact gap PR #4's bare warning left open.
    """
    conf = op.get("confidence")
    if not isinstance(conf, (int, float)):
        return None
    mark = "🟢" if conf >= 60 else ("🟡" if conf >= 30 else "🔴")
    line = f"  {mark} 현실성 {conf:.0f}/100"
    ex = op.get("executable_sets")
    net = op.get("net_total_edge")
    if isinstance(ex, (int, float)) and ex > 0 and isinstance(net, (int, float)):
        line += f" (실행가능 {ex:.0f}세트 · 순차익 {_usd(net)})"
    elif op.get("feasible_min_order") is False:
        line += " (실행 어려움 — $1 최소주문 미달)"
    return line


def _wc_value_line(op: dict) -> Optional[str]:
    """'$1 → 약 $X 가치' for a value bet — the user's own mental model.

    Buying at ``price`` when the consensus fair probability is ``fair_prob``
    means each $1 staked is worth ``fair/price`` at fair odds. A +10% edge reads
    as '$1 → 약 $1.10 가치', which is exactly how the ask was phrased.
    """
    price = op.get("price")
    fair = op.get("fair_prob")
    if not isinstance(price, (int, float)) or not isinstance(fair, (int, float)):
        return None
    if price <= 0:
        return None
    value = fair / price
    return f"  💵 $1 → 약 {_usd(value)} 가치 ({_cents(price)}에 매수, 공정 {_cents(fair)})"


def _min_buyin_lines(op: dict) -> list:
    """Minimum balanced buy-in under Polymarket's $1-per-order floor.

    A risk-free complete set needs the SAME share count on every leg, so you
    can't just put $1 on each — unequal shares break the hedge. The cheapest leg
    is the one that struggles to clear the $1 minimum order, so it sets the
    floor: ``K = ceil(1 / cheapest_ask)`` shares of every leg.

    We then compare K to the top-of-book depth (``max_sets``). When the $1 floor
    needs more shares than the book offers at these prices, buying up to the
    minimum walks the book and the edge evaporates — so the alert says so
    instead of implying a clean fill.
    """
    if op.get("kind") != "BUY_SET":
        return []
    legs = op.get("legs") or []
    asks = [leg.get("ask") for leg in legs]
    asks = [a for a in asks if isinstance(a, (int, float)) and a > 0]
    if not asks or len(asks) != len(legs):
        return []
    cheapest = min(asks)
    cost_per_set = op.get("cost_per_set")
    if not isinstance(cost_per_set, (int, float)) or cost_per_set <= 0:
        cost_per_set = sum(asks)
    k_min = math.ceil(1.0 / cheapest)
    out = [
        f"  💰 최소 매수: 각 {k_min}주 (가장 싼 결과 {_cents(cheapest)} 기준) "
        f"= 약 {_usd(k_min * cost_per_set)}"
    ]
    max_sets = op.get("max_sets")
    if isinstance(max_sets, (int, float)) and max_sets > 0:
        if k_min > max_sets:
            out.append(
                f"  ⚠️ 호가 깊이 약 {int(max_sets)}주뿐 → "
                "최소주문 $1 맞추면 차익 소멸 (실행 어려움)"
            )
        else:
            edge_per_set = op.get("edge_per_set")
            if isinstance(edge_per_set, (int, float)):
                out.append(
                    f"     이 물량 차익 ≈ {_usd(k_min * edge_per_set)} (실행 가능)"
                )
            else:
                out.append("     (호가 깊이 내 — 실행 가능)")
    return out


def compute_notification(
    payload: dict,
    seen_keys,
    include_ev: bool = False,
    min_edge_pct: float = 0.0,
    min_confidence: float = 0.0,
    max_days_to_resolution: Optional[float] = None,
) -> tuple[Optional[str], list]:
    """Return (message_or_None, new_seen_keys) given a payload and prior state.

    ``new_seen_keys`` is reset to whatever is currently live (JSON-friendly
    lists), so disappeared edges re-fire when they come back.

    ``min_confidence`` drops risk-free ops below a realism score (0-100) so the
    feed shows executable money, not paper edges. Ops without a score (legacy
    payloads) default to 0 and pass only when ``min_confidence`` is 0.

    ``max_days_to_resolution`` keeps only opportunities that settle within that
    many days (instant ``MINT_SELL`` always passes) — for focusing on near-dated
    edges where capital isn't locked up. ``None`` disables the window.
    """
    poly = [
        o for o in payload.get("polymarket", [])
        if o.get("edge_pct", 0) >= min_edge_pct
        and o.get("confidence", 0) >= min_confidence
        and _resolves_soon(o, max_days_to_resolution)
    ]
    cross = [
        o for o in payload.get("cross_venue", [])
        if o.get("edge_pct", 0) >= min_edge_pct
        and _resolves_soon(o, max_days_to_resolution)
    ]
    ev = [o for o in (payload.get("ev", []) if include_ev else [])
          if _resolves_soon(o, max_days_to_resolution)]
    world_cup = [o for o in payload.get("world_cup", [])
                 if _resolves_soon(o, max_days_to_resolution)]
    # Favorites carry their own resolution window from the scanner, but honor an
    # extra notify-level window too if one is set.
    favorites = [o for o in payload.get("favorites", [])
                 if _resolves_soon(o, max_days_to_resolution)]

    current: dict[tuple, tuple] = {}
    for op in poly:
        current[_poly_key(op)] = ("poly", op)
    for op in cross:
        current[_cross_key(op)] = ("cross", op)
    for op in ev:
        current[_ev_key(op)] = ("ev", op)
    for op in world_cup:
        current[_wc_key(op)] = ("wc", op)
    for op in favorites:
        current[_fav_key(op)] = ("fav", op)

    seen = {tuple(k) for k in seen_keys}
    new_keys = [k for k in current if k not in seen]
    new_seen = [list(k) for k in current]
    if not new_keys:
        return None, new_seen

    new_poly = [current[k][1] for k in new_keys if k[0] == "poly"]
    new_cross = [current[k][1] for k in new_keys if k[0] == "cross"]
    new_ev = [current[k][1] for k in new_keys if k[0] == "ev"]
    new_wc = [current[k][1] for k in new_keys if k[0] == "wc"]
    new_fav = [current[k][1] for k in new_keys if k[0] == "fav"]
    # Order the feed by *realism*, not the biggest paper edge: feasible first,
    # then confidence x net edge (falls back to edge_pct for legacy payloads
    # without a realism score). Mirrors detect.rank_key.
    def _poly_rank(o: dict) -> tuple:
        conf = o.get("confidence", 0) or 0
        net = o.get("net_total_edge", 0) or 0
        value = (conf / 100.0) * net if (conf or net) else o.get("edge_pct", 0)
        return (1 if o.get("feasible_min_order", True) else 0, value)

    new_poly.sort(key=_poly_rank, reverse=True)
    new_cross.sort(key=lambda o: o.get("total_edge", 0), reverse=True)
    new_ev.sort(key=lambda o: o.get("ev_per_contract", 0), reverse=True)
    new_wc.sort(key=lambda o: o.get("ev_per_contract", 0), reverse=True)
    # Favorites: soonest-to-resolve first, then highest payout.
    new_fav.sort(key=lambda o: (
        o.get("days_to_resolution") if o.get("days_to_resolution") is not None else 1e9,
        -(o.get("payout_multiple", 0) or 0),
    ))

    src = payload.get("meta", {}).get("source", "demo")
    src_ko = {"live": "실시간", "demo": "데모", "error": "오류"}.get(src, src)
    only = lambda *others: not any(others)  # noqa: E731
    if new_wc and only(new_poly, new_cross, new_ev, new_fav):
        header = "⚽ 월드컵 가치베팅"
    elif new_fav and only(new_poly, new_cross, new_ev, new_wc):
        header = "💵 곧 끝나는 유력후보 (무위험 아님)"
    else:
        header = "\U0001f514 새 차익거래"
    lines = [f"{header} ({src_ko}):"]
    if new_poly:
        lines.append("폴리마켓:")
        for o in new_poly:
            apr = "즉시" if o.get("annualized_pct") is None else f"연 {o['annualized_pct']:.0f}%"
            action = ("모든 결과 매수 후 정산 시 $1 회수"
                      if o.get("kind") == "BUY_SET"
                      else "세트를 $1에 만들어 모든 결과 즉시 매도 (즉시 정산)")
            cap = o.get("capital_required")
            cap_str = f" (자본 {_usd(cap)})" if cap else ""
            # 자본이 묶이는 BUY_SET에만 정산 기간 표시 (MINT_SELL은 즉시 정산).
            eta = _resolution_eta(o.get("end_date")) if o.get("annualized_pct") is not None else None
            eta_str = f" · {eta}" if eta else ""
            lines.append(f"- {o.get('question','')[:48]} — {action}")
            lines.append(
                f"  보장수익 {o.get('edge_pct',0):.2f}% ({apr}) · "
                f"예상수익 {_usd(o.get('total_edge',0))}{cap_str}{eta_str}"
            )
            realism = _realism_line(o)
            if realism:
                lines.append(realism)
            lines.extend(_buy_list_lines(o))
            lines.extend(_min_buyin_lines(o))
            link = _link_line(o)
            if link:
                lines.append(link)
    if new_cross:
        lines.append("크로스 거래소:")
        for o in new_cross:
            lines.append(
                f"- {o.get('question','')[:38]} — {o.get('yes_venue')}에서 YES "
                f"{o.get('yes_price',0):.2f} + {o.get('no_venue')}에서 NO "
                f"{o.get('no_price',0):.2f} 매수"
            )
            eta = _resolution_eta(o.get("end_date"))
            eta_str = f" · {eta}" if eta else ""
            lines.append(
                f"  보장수익 {o.get('edge_pct',0):.2f}% · "
                f"예상수익 {_usd(o.get('total_edge',0))}{eta_str}"
            )
    if new_ev:
        lines.append("포지티브 EV (무위험 아님):")
        for o in new_ev:
            lines.append(
                f"- {o.get('question','')[:40]} — {o.get('venue')}에서 "
                f"{o.get('side')} {o.get('price',0):.2f}에 매수 "
                f"(공정확률 {o.get('fair_prob',0):.2f})"
            )
            eta = _resolution_eta(o.get("end_date"))
            eta_str = f" · {eta}" if eta else ""
            lines.append(
                f"  기대우위 {o.get('edge_pct',0):.1f}% · "
                f"1주당 기대값 {o.get('ev_per_contract',0):+.3f}{eta_str}"
            )
            link = _link_line(o)
            if link:
                lines.append(link)
    if new_wc:
        lines.append("월드컵 가치 (무위험 아님 — 북메이커 컨센서스 대비):")
        for o in new_wc:
            lines.append(
                f"- {o.get('question','')[:48]} — {o.get('side')} "
                f"{o.get('price',0):.2f}에 매수 (컨센서스 공정확률 {o.get('fair_prob',0):.2f})"
            )
            eta = _resolution_eta(o.get("end_date"))
            eta_str = f" · {eta}" if eta else ""
            lines.append(
                f"  기대우위 {o.get('edge_pct',0):.0f}% · "
                f"1주당 기대값 {o.get('ev_per_contract',0):+.3f}{eta_str}"
            )
            value = _wc_value_line(o)
            if value:
                lines.append(value)
            link = _link_line(o)
            if link:
                lines.append(link)
    if new_fav:
        lines.append("곧 끝나는 유력후보 (무위험 아님 — 빗나가면 전액 손실):")
        for o in new_fav:
            price = o.get("price", 0) or 0
            payout = o.get("payout_multiple") or (1.0 / price if price else 0)
            prob = o.get("implied_prob", price) or 0
            gain_pct = (payout - 1.0) * 100
            lines.append(
                f"- {o.get('question','')[:48]} — {o.get('outcome','')[:24]} "
                f"{_cents(price)}에 매수"
            )
            eta = _resolution_eta(o.get("end_date"))
            eta_str = f" · {eta}" if eta else ""
            lines.append(
                f"  💵 $1 → 약 {_usd(payout)} (적중 시 +{gain_pct:.0f}%){eta_str}"
            )
            lines.append(
                f"  적중 확률 ≈ {prob*100:.0f}% (시장가 기준) · 빗나가면 전액 손실"
            )
            link = _link_line(o)
            if link:
                lines.append(link)

    # 용어 풀이 — 메시지에 실제로 쓰인 용어만 각주로.
    glossary = []
    if new_poly or new_cross:
        glossary.append(
            "ℹ️ 보장수익(edge)=투입 자본 대비 무조건 남는 비율 "
            "(차익거래는 무위험, 정산까지 자본이 묶임)"
        )
    if new_ev or new_wc:
        glossary.append(
            "ℹ️ 기대우위(edge)=공정확률보다 싸게 산 정도. 평균적으로 유리할 뿐, "
            "무위험 아님 — 한 판은 전액 잃을 수 있음"
        )
    if new_fav:
        glossary.append(
            "ℹ️ 유력후보=시장가가 곧 끝나는 결과를 높은 확률로 보는 것. 차익도 우위도 "
            "아님 — 가격이 곧 승률이라, 적중 시 소액(+10%대) 이익·빗나가면 전액 손실"
        )
    if glossary:
        lines.append("")
        lines.extend(glossary)

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
    return {"polymarket": [], "cross_venue": [], "ev": [], "world_cup": [],
            "favorites": [], "meta": meta}


def build_favorites_payload(demo: bool = False) -> dict:
    """Soon-resolving favorites ("$1 -> ~$1.1 if it wins"), in alert shape.

    NOT risk-free — see ``favorites.py``. Any market (not just sports): an
    outcome priced in the favorite band that settles within ``NOTIFY_MAX_DAYS``.
    Env knobs: NOTIFY_FAV_MIN_PRICE / NOTIFY_FAV_MAX_PRICE (payout band),
    NOTIFY_FAV_MIN_SIZE (depth), NOTIFY_MAX_DAYS (window). A live scan failure
    returns a ``meta.source == "error"`` payload so the caller skips it.
    """
    from .favorites import favorite_to_dict

    min_price = float(os.environ.get("NOTIFY_FAV_MIN_PRICE", "0.80") or "0.80")
    max_price = float(os.environ.get("NOTIFY_FAV_MAX_PRICE", "0.91") or "0.91")
    min_size = float(os.environ.get("NOTIFY_FAV_MIN_SIZE", "5") or "5")
    max_days_raw = os.environ.get("NOTIFY_MAX_DAYS", "2").strip()
    max_days = float(max_days_raw) if max_days_raw else 2.0
    try:
        if demo:
            from .demo import load_demo_favorites

            bets = load_demo_favorites(
                min_price=min_price, max_price=max_price,
                min_size=min_size, max_days=max_days,
            )
            meta = {"source": "demo"}
        else:
            from .client import PolymarketClient
            from .favorites import build_favorites_live

            debug = os.environ.get("NOTIFY_FAV_DEBUG", "").lower() in ("1", "true", "yes")
            bets = build_favorites_live(
                PolymarketClient(), min_price=min_price, max_price=max_price,
                min_size=min_size, max_days=max_days, debug=debug,
            )
            meta = {"source": "live"}
    except Exception as exc:  # noqa: BLE001 - never alert on a failed scan
        return _empty_payload({"source": "error", "error": str(exc)[:200]})

    payload = _empty_payload(meta)
    payload["favorites"] = [favorite_to_dict(b) for b in bets]
    return payload


def build_world_cup_payload(demo: bool = False) -> dict:
    """Per-MATCH World Cup value bets vs bookmaker consensus, in alert shape.

    These are near-dated (settle right after the match), so they pair with the
    ``NOTIFY_MAX_DAYS`` window — unlike the tournament-outright path, which
    settles only at the final. Demo uses the bundled fixture. Live needs
    ODDS_API_KEY; any failure (no key, egress, API change) returns a
    ``meta.source == "error"`` payload so the caller skips sending bad data.
    """
    from .scanner import ev_to_dict

    min_edge = float(os.environ.get("NOTIFY_WC_MIN_EDGE", "0.05") or "0.05")
    try:
        if demo:
            from .demo import load_demo_world_cup_matches

            ops = load_demo_world_cup_matches(min_edge=min_edge)
            meta = {"source": "demo"}
        else:
            key = os.environ.get("ODDS_API_KEY")
            if not key:
                return _empty_payload({"source": "error", "error": "ODDS_API_KEY not set"})
            from .client import PolymarketClient
            from .multivenue import scan_world_cup_match_value_live
            from .odds_api import OddsApiClient

            ops = scan_world_cup_match_value_live(
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
    elif mode == "favorites":
        payload = build_favorites_payload(demo=demo)
        if payload.get("meta", {}).get("source") == "error":
            print(f"favorites scan unavailable ({payload['meta'].get('error')}); "
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
    min_conf = float(os.environ.get("NOTIFY_MIN_CONFIDENCE", "0") or "0")
    max_days_raw = os.environ.get("NOTIFY_MAX_DAYS", "").strip()
    max_days = float(max_days_raw) if max_days_raw else None

    text, new_seen = compute_notification(
        payload, load_state(), include_ev=include_ev, min_edge_pct=min_edge,
        min_confidence=min_conf, max_days_to_resolution=max_days,
    )
    save_state(new_seen)

    if text is None:
        print("No new opportunities since last run.")
        return 0
    note = maybe_gemini_note(payload)
    if note:
        text += f"\n\n\U0001f916 Gemini: {note}"
    if demo:
        text = "[데모 테스트 — 실제 데이터 아님]\n" + text
    send_telegram(token, chat_id, text)
    print("Notification sent.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
