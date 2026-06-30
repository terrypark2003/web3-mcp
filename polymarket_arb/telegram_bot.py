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
    def fav_scan_fn():
        from .client import PolymarketClient
        from .favorites import build_favorites_live

        return build_favorites_live(
            PolymarketClient(),
            # payout band 1.05x–1.15x  ->  price 0.87–0.95
            min_price=float(os.environ.get("NOTIFY_FAV_MIN_PRICE", "0.87") or "0.87"),
            max_price=float(os.environ.get("NOTIFY_FAV_MAX_PRICE", "0.95") or "0.95"),
            min_size=float(os.environ.get("NOTIFY_FAV_MIN_SIZE", "5") or "5"),
            max_days=float(os.environ.get("NOTIFY_MAX_DAYS", "1") or "1"),
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
    """Turn ``[[(label, callback_data), ...], ...]`` into an InlineKeyboardMarkup."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows
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

    async def on_message(update, context):  # pragma: no cover - requires Telegram
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return
        reply = bot.handle(chat.id, message.text or "")
        await message.reply_text(reply)

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

    # Only the favorites feed is proactive now (other scan pushes removed per
    # request). /scan, /cross, /ev still work on demand.
    fav_interval = float(os.environ.get("FAV_INTERVAL_SEC", "900") or "900")

    async def favorites_job(context):  # pragma: no cover - requires Telegram + network
        try:
            result = bot.poll_favorites()
        except Exception as exc:  # noqa: BLE001 - a scan failure shouldn't kill the job
            print(f"favorites poll failed: {exc}")
            return
        if result:
            message, rows = result
            await context.bot.send_message(
                chat_id=bot.owner_id, text=message, reply_markup=_keyboard(rows),
            )

    if app.job_queue is not None:
        # first=20s so the first favorites alert lands shortly after boot.
        app.job_queue.run_repeating(favorites_job, interval=fav_interval, first=20)
    else:  # pragma: no cover
        print("job-queue extra not installed; favorites alerts disabled "
              "(pip install 'python-telegram-bot[job-queue]').")

    print(f"Bot up. mode={bot.exec_config.mode}. Waiting for owner {bot.owner_id}.")
    app.run_polling()


if __name__ == "__main__":  # pragma: no cover
    main()
