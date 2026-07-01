"""Thin Telegram shell over ArbBot. Run this on YOUR machine, not here.

    pip install -r requirements-bot.txt
    cp .env.example .env        # fill in your secrets (never commit .env)
    set -a; . ./.env; set +a    # load env vars
    python -m polymarket_arb.telegram_bot

All configuration comes from environment variables (see .env.example). The bot
ignores every chat except TELEGRAM_OWNER_ID and starts in dry-run unless
EXECUTION_MODE=live. This file is intentionally minimal; the testable logic
lives in bot_core.py.
"""

from __future__ import annotations

import asyncio
import os

from .bot_core import ArbBot
from .detect import FeeModel
from .execution import ExecutionConfig, PolymarketExecutor


def build_bot() -> ArbBot:
    owner_raw = os.environ.get("TELEGRAM_OWNER_ID")
    if not owner_raw:
        raise SystemExit("TELEGRAM_OWNER_ID is required (your numeric Telegram id).")
    owner_id = int(owner_raw)

    config = ExecutionConfig.from_env()
    executor = PolymarketExecutor(config)
    fee = FeeModel()

    def scan_fn():
        # Live scan runs on the deploying machine, which must reach Polymarket.
        from .client import PolymarketClient
        from .scanner import scan_live

        return scan_live(PolymarketClient(), fee)

    def cross_scan_fn():
        from .client import PolymarketClient
        from .kalshi_client import KalshiClient
        from .multivenue import scan_cross_venue_live

        return scan_cross_venue_live(KalshiClient(), PolymarketClient())

    # EV scanning needs a fair-value source; only enable it if one is supplied.
    ev_scan_fn = None
    fair_path = os.environ.get("FAIR_VALUES_FILE")
    if fair_path:
        def ev_scan_fn():  # noqa: F811 - conditional definition is intentional
            import json

            from .client import PolymarketClient
            from .ev import fair_value_from_map
            from .multivenue import scan_ev_live

            with open(fair_path, encoding="utf-8") as fh:
                data = json.load(fh)
            probs = data.get("fair_values", data)
            fair = fair_value_from_map({k: float(v) for k, v in probs.items()})
            return scan_ev_live(PolymarketClient(), fair)

    channel_raw = os.environ.get("SIGNAL_CHANNEL_ID")
    signal_channel_id = int(channel_raw) if channel_raw else None

    # World Cup value scan for /ask context (needs the odds API).
    wc_scan_fn = None
    odds_key = os.environ.get("ODDS_API_KEY")
    if odds_key:
        def wc_scan_fn():  # noqa: F811 - conditional definition is intentional
            from .client import PolymarketClient
            from .multivenue import scan_world_cup_value_live
            from .odds_api import OddsApiClient

            return scan_world_cup_value_live(PolymarketClient(), OddsApiClient(odds_key))

    # Near-resolution favorites ("tap to buy $1"). Whole-pool, no odds key needed.
    # NOTIFY_FAV_EXCLUDE: comma-separated question substrings to drop (unset =
    # default crypto filter; empty string = keep everything).
    _excl_raw = os.environ.get("NOTIFY_FAV_EXCLUDE")
    _exclude_terms = (
        None if _excl_raw is None
        else [t.strip().lower() for t in _excl_raw.split(",") if t.strip()]
    )

    def fav_scan_fn():
        from .client import PolymarketClient
        from .favorites import build_favorites_live

        return build_favorites_live(
            PolymarketClient(),
            # payout band 1.05x–1.15x  ->  price 0.87–0.95
            min_price=float(os.environ.get("NOTIFY_FAV_MIN_PRICE", "0.87") or "0.87"),
            max_price=float(os.environ.get("NOTIFY_FAV_MAX_PRICE", "0.95") or "0.95"),
            min_size=float(os.environ.get("NOTIFY_FAV_MIN_SIZE", "5") or "5"),
            # 0.5d = 12h, the widest bucket; favorites_now buckets into 3/6/9/12h.
            max_days=float(os.environ.get("NOTIFY_MAX_DAYS", "0.5") or "0.5"),
            # Only price the soonest N candidates (keeps /fav fast); "더보기" pages
            # through them. Raise it if you want more pages.
            book_limit=int(os.environ.get("NOTIFY_FAV_BOOK_LIMIT", "120") or "120"),
            exclude_terms=_exclude_terms,  # default: drop crypto Up/Down gambles
        )

    # Gemini = plain-language analyst over the real signals (never the oracle).
    gemini_generate = None
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        from .gemini import GeminiClient

        gemini_generate = GeminiClient(gemini_key).generate

    return ArbBot(
        owner_id=owner_id,
        scan_fn=scan_fn,
        executor=executor,
        exec_config=config,
        fee_model=fee,
        min_alert_edge_pct=float(os.environ.get("ALERT_MIN_EDGE_PCT", "0") or "0"),
        alerts_enabled=(os.environ.get("ALERTS_ENABLED", "true").lower() != "false"),
        cross_scan_fn=cross_scan_fn,
        ev_scan_fn=ev_scan_fn,
        signal_channel_id=signal_channel_id,
        wc_scan_fn=wc_scan_fn,
        gemini_generate=gemini_generate,
        fav_scan_fn=fav_scan_fn,
        fav_max_buy_usd=float(os.environ.get("FAV_BUY_USD", "1") or "1"),
    )


def _keyboard(rows):  # pragma: no cover - thin telegram adapter
    """Turn ``[[(label, value), ...], ...]`` into an InlineKeyboardMarkup.

    A value starting with ``http`` becomes a URL button (opens the link — e.g. a
    Polymarket market to buy manually); anything else is a callback button.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def button(label, value):
        if value.startswith("http://") or value.startswith("https://"):
            return InlineKeyboardButton(label, url=value)
        return InlineKeyboardButton(label, callback_data=value)

    return InlineKeyboardMarkup([
        [button(label, value) for label, value in row] for row in rows
    ])


def main() -> None:
    try:
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "python-telegram-bot not installed (pip install -r requirements-bot.txt)"
        ) from exc

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required.")

    bot = build_bot()
    app = Application.builder().token(token).build()

    # /favN -> show favorites resolving within N hours (top 5, soonest first).
    _FAV_WINDOWS = {
        "/fav1": 1, "/fav3": 3, "/fav6": 6, "/fav9": 9, "/fav12": 12,
        "/fav": 12, "/favorites": 12,
    }

    async def _send_favorites(chat_id, hours):  # pragma: no cover - Telegram + network
        await app.bot.send_message(chat_id=chat_id, text=f"⏳ {hours}시간 내 유력후보 찾는 중…")
        # The scan hits the network; run it off the event loop so polling stays live.
        try:
            chunks = await asyncio.to_thread(bot.favorites_now, 5, float(hours))
        except Exception as exc:  # noqa: BLE001 - surface scan errors instead of silence
            await app.bot.send_message(chat_id=chat_id, text=f"후보 조회 오류: {exc}")
            return
        if not chunks:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"지금 {hours}시간 내 정산 + 가격 0.87~0.95 조건에 맞는 후보가 없어요.",
            )
            return
        for message, rows in chunks:
            await app.bot.send_message(
                chat_id=chat_id, text=message, reply_markup=_keyboard(rows),
            )

    async def on_message(update, context):  # pragma: no cover - requires Telegram
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return
        text = message.text or ""
        cmd = text.strip().split()[0].lower() if text.strip() else ""
        if cmd in _FAV_WINDOWS:
            if not bot.is_authorized(chat.id):
                await message.reply_text("Unauthorized.")
                return
            await _send_favorites(chat.id, _FAV_WINDOWS[cmd])
            return
        await message.reply_text(bot.handle(chat.id, text))

    async def on_callback(update, context):  # pragma: no cover - requires Telegram
        query = update.callback_query
        chat = update.effective_chat
        if query is None or chat is None:
            return
        await query.answer()
        reply, rows = bot.handle_callback(chat.id, query.data or "")
        markup = _keyboard(rows) if rows else None
        await context.bot.send_message(chat_id=chat.id, text=reply, reply_markup=markup)

    app.add_handler(MessageHandler(filters.TEXT, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))

    # On-demand only: favorites are shown when the owner sends /fav (no proactive
    # crawling/pushing). /scan, /cross, /ev also work on demand.
    print(f"Bot up. mode={bot.exec_config.mode}. Waiting for owner {bot.owner_id}.")
    app.run_polling()


if __name__ == "__main__":  # pragma: no cover
    main()
