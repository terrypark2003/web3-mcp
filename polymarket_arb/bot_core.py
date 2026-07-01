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
    ExecutionError,
    OrderPlan,
    PolymarketExecutor,
    build_order_plan,
    build_single_buy_plan,
    simulate,
)
from .models import ARB_BUY_SET, Opportunity
from .portfolio import SizingConfig, allocate_portfolio, format_portfolio
from .scanner import cross_to_dict, ev_to_dict, opportunity_to_dict
from .settlements import check_settlements, format_settlement_message

HELP = (
    "예측시장 봇 (소유자 전용)\n"
    "💵 /fav1 /fav3 /fav6 /fav9 /fav12 — 해당 시간 내 정산되는 "
    "$1→약 $1.05~$1.15 '유력후보' 5개를 임박 순으로 보여줍니다. "
    "번호 버튼 [💵 $1 매수]는 확인 후 자동 매수(기본 dry-run, "
    "EXECUTION_MODE=live일 때 실제 체결 — 이메일 계정도 공식 SDK로 지원), "
    "[🔗 열기]는 그 마켓을 폴리마켓에서 엽니다. [➕ 더보기]로 다음 5개씩.\n"
    "\n"
    "명령어:\n"
    "/fav1 - 1시간 내 정산 유력후보 (가장 임박)\n"
    "/fav3 - 3시간 내 / /fav6 - 6시간 내 / /fav9 - 9시간 내 / /fav12 - 12시간 내\n"
    "/balance - 내 폴리마켓 현금(pUSD) 잔고 확인\n"
    "/portfolio - 총 자산(현금 + 포지션 평가액) + 미실현 손익\n"
    "🔔 보유 포지션이 정산되면(적중/낙첨) 자동으로 알려드려요 (POLYMARKET_FUNDER 설정 시).\n"
    "/status - 모드·최대 금액·실거래 준비 상태\n"
    "/scan - 폴리마켓 컴플리트셋 차익 찾기\n"
    "/cross - 거래소 간 차익 (Kalshi vs 폴리마켓)\n"
    "/ev - 공정가치 대비 포지티브 EV (무위험 아님)\n"
    "/ask <질문> - 현재 신호에 대해 Gemini에게 질문\n"
    "/allocate <자본> - 엣지에 자본 배분\n"
    "/plan <market_id> - 한 기회의 주문 계획 보기\n"
    "/execute <market_id> - 거래 스테이징 후 /confirm 으로 실행\n"
    "/confirm - 스테이징한 거래 실행 (live만 실제, 아니면 dry-run)\n"
    "/cancel - 스테이징 취소"
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
        fav_scan_fn: Optional[Callable[[], list]] = None,
        fav_max_buy_usd: float = 1.0,
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
        self.fav_scan_fn = fav_scan_fn
        self.fav_max_buy_usd = fav_max_buy_usd
        self._pending: dict[int, OrderPlan] = {}
        self._alert_seen: set = set()
        self._broadcast_seen: set = set()
        self._fav_offered: dict[str, object] = {}  # short id -> FavoriteBet
        self._fav_counter: int = 0
        self._fav_page: dict | None = None  # last /fav scan, paged via "더보기"
        self._settlement_state: dict = {}  # see settlements.check_settlements

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
        if cmd in ("/balance", "/bal"):
            return self._balance()
        if cmd in ("/portfolio", "/worth", "/pf"):
            return self._portfolio()
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

    # -- Favorites: "tap to buy $1" (NOT risk-free) ----------------------- #

    def favorites_now(self, limit: int = 5, max_hours: float = 12.0) -> list:
        """On-demand: soonest-resolving favorites within ``max_hours``, paged by 5.

        Scans once, caches the full sorted list, and returns ``[(message, rows)]``
        for the first ``limit`` items. A "더보기" button pages through the rest via
        ``handle_callback("fav_more")`` — no re-scan. Each item gets TWO buttons:
        a numbered [$1 매수] callback (two-step confirm, dry-run unless
        EXECUTION_MODE=live — works for deposit-wallet/email accounts via the
        unified polymarket-client SDK) and a [🔗] link that opens the market on
        Polymarket for manual buying. NOT deduped. Empty list if none qualify.
        """
        if self.fav_scan_fn is None:
            return []
        favs = self.fav_scan_fn() or []
        timed = []
        for f in favs:
            hours = f.days_to_resolution * 24 if f.days_to_resolution is not None else None
            if hours is not None and 0 < hours <= max_hours:
                timed.append((hours, f))
        timed.sort(key=lambda x: x[0])  # soonest first
        if not timed:
            self._fav_page = None
            return []
        # Cache the whole sorted list so "더보기" can page without re-scanning.
        self._fav_page = {"hours": max_hours, "items": timed, "offset": 0, "limit": limit}
        return [self._render_fav_page()]

    def _render_fav_page(self) -> tuple:
        """Render the current page of the cached favorites and advance the offset."""
        page = self._fav_page
        items, off, limit = page["items"], page["offset"], page["limit"]
        chunk = items[off:off + limit]
        total = len(items)
        lines = [
            f"💵 {page['hours']:g}시간 내 유력후보 {off + 1}–{off + len(chunk)}/{total} "
            f"(무위험 아님 — 번호 버튼=$1 매수(확인 필요), 🔗=폴리마켓에서 열기):"
        ]
        rows = []
        for i, (hours, f) in enumerate(chunk, start=off + 1):
            self._fav_counter += 1
            ref = f"f:{self._fav_counter}"
            self._fav_offered[ref] = f
            payout = (1.0 / f.price) if f.price else 0.0
            cents = f.price * 100
            lines.append(
                f"{i}) {f.question[:38]} — {f.outcome[:12]} {cents:.0f}¢ "
                f"($1→약 ${payout:.2f}, ⏳ {hours:.1f}시간 남음)"
            )
            market_url = f.url or "https://polymarket.com/markets"
            rows.append([
                (f"{i}) 💵 $1 매수 — {f.outcome[:10]} {cents:.0f}¢", ref),
                ("🔗 열기", market_url),
            ])
        lines.append("⚠️ 무위험 아님: 적중 시 소액 이익, 빗나가면 전액 손실.")
        page["offset"] = off + len(chunk)
        if page["offset"] < total:
            remaining = total - page["offset"]
            nxt = min(limit, remaining)
            rows.append([(f"➕ 더보기 (다음 {nxt}개 · 남은 {remaining}개)", "fav_more")])
        return "\n".join(lines), rows

    def handle_callback(self, chat_id: int, data: str):
        """Handle an inline-button tap. Returns ``(reply_text, button_rows|None)``.

        Two-step for safety: a favorite button stages a $1 buy and shows what
        would be sent; a second 'confirm' tap actually places it (dry-run unless
        ``EXECUTION_MODE=live``).
        """
        if not self.is_authorized(chat_id):
            return "Unauthorized.", None
        data = (data or "").strip()

        if data == "fav_more":
            if not self._fav_page or self._fav_page["offset"] >= len(self._fav_page["items"]):
                return "더 보여줄 후보가 없어요. /fav 로 다시 조회하세요.", None
            return self._render_fav_page()

        if data == "fav_cancel":
            self._pending.pop(chat_id, None)
            return "취소했습니다.", None

        if data == "fav_confirm":
            return self._confirm(chat_id), None

        if data.startswith("f:"):
            fav = self._fav_offered.get(data)
            if fav is None:
                return "만료된 항목입니다. 다음 알림에서 다시 시도하세요.", None
            try:
                plan = build_single_buy_plan(
                    fav.token_id, fav.outcome, fav.price,
                    market_id=fav.market_id, question=fav.question,
                    dollars=self.fav_max_buy_usd, slippage=self.exec_config.slippage,
                )
            except Exception as exc:  # noqa: BLE001
                return f"주문을 만들 수 없습니다: {exc}", None
            self._pending[chat_id] = plan
            verb = ("실제로 체결" if self.exec_config.mode == "live"
                    else "시뮬레이션(dry-run)")
            text = (
                f"{simulate(plan, self.exec_config)}\n\n"
                f"⚠️ 무위험 아님 — 빗나가면 전액 손실.\n아래 확인을 누르면 {verb} 합니다."
            )
            return text, [[("✅ 확인", "fav_confirm")], [("✖️ 취소", "fav_cancel")]]

        return f"알 수 없는 동작: {data}", None

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

    def _balance(self) -> str:
        if not self.exec_config.funder:
            return ("잔고를 조회하려면 POLYMARKET_FUNDER(폴리마켓 입금 주소)가 "
                    "필요합니다 (Railway 변수에 설정).")
        try:
            bal = self.executor.usdc_balance()
        except ExecutionError as exc:
            return f"잔고 조회 불가: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface API/network errors to chat
            return f"잔고 조회 오류: {exc}"
        return f"💰 폴리마켓 USDC 잔고: ${bal:,.2f}"

    def _portfolio(self) -> str:
        if not self.exec_config.funder:
            return ("포트폴리오를 보려면 POLYMARKET_FUNDER(폴리마켓 입금 주소)가 "
                    "필요합니다 (Railway/Fly 변수에 설정).")
        try:
            pf = self.executor.portfolio()
        except ExecutionError as exc:
            return f"포트폴리오 조회 불가: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface API/network errors to chat
            return f"포트폴리오 조회 오류: {exc}"

        cash = pf["cash"]
        if cash is None:
            lines = [f"💼 포지션 평가액: ${pf['positions_value']:,.2f} "
                     f"(현금 조회 실패 — 총합에 미포함)"]
        else:
            lines = [f"💼 포트폴리오 총 가치: ${pf['total']:,.2f}",
                     f"  ├ 현금(pUSD): ${cash:,.2f}",
                     f"  └ 포지션 {pf['count']}개: ${pf['positions_value']:,.2f}"]
        sign = "+" if pf["pnl"] >= 0 else "-"
        lines.append(f"  📈 미실현 손익: {sign}${abs(pf['pnl']):,.2f}")
        for p in pf["positions"][:5]:
            tag = " · 정산가능" if p["redeemable"] else ""
            lines.append(f"• {p['title'][:32]} — {p['outcome'][:10]} "
                         f"${p['value']:,.2f}{tag}")
        if not pf["positions"]:
            lines.append("(보유 포지션 없음)")
        return "\n".join(lines)

    def check_settlements_now(self) -> list[str]:
        """Poll positions and return messages for newly-resolved ones (may be []).

        Stateful across calls via ``self._settlement_state`` (persist it to
        survive restarts — see ``settlements.load_state``/``save_state``).
        Silent no-op (returns []) if ``POLYMARKET_FUNDER`` isn't configured.
        """
        if not self.exec_config.funder:
            return []
        raw = self.executor.raw_positions()  # ExecutionError propagates to caller
        events, self._settlement_state = check_settlements(raw, self._settlement_state)
        return [format_settlement_message(e) for e in events]

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
