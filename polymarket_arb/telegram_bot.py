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
    )


def main() -> None:
    try:
        from telegram.ext import Application, MessageHandler, filters
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

    app.add_handler(MessageHandler(filters.TEXT, on_message))

    # Proactive alerts: poll on an interval and push new arbs to the owner.
    interval = float(os.environ.get("ALERT_INTERVAL_SEC", "300") or "300")

    async def alert_job(context):  # pragma: no cover - requires Telegram + network
        try:
            message = bot.poll_alerts()
        except Exception as exc:  # noqa: BLE001 - a scan failure shouldn't kill the job
            print(f"alert poll failed: {exc}")
            return
        if message:
            await context.bot.send_message(chat_id=bot.owner_id, text=message)

    async def broadcast_job(context):  # pragma: no cover - requires Telegram + network
        try:
            message = bot.poll_broadcast()
        except Exception as exc:  # noqa: BLE001 - a scan failure shouldn't kill the job
            print(f"broadcast poll failed: {exc}")
            return
        if message and bot.signal_channel_id is not None:
            await context.bot.send_message(chat_id=bot.signal_channel_id, text=message)

    if app.job_queue is not None:
        app.job_queue.run_repeating(alert_job, interval=interval, first=interval)
        if bot.signal_channel_id is not None:
            app.job_queue.run_repeating(
                broadcast_job, interval=interval, first=interval
            )
            print(f"Broadcasting risk-free edges to channel {bot.signal_channel_id}.")
    else:  # pragma: no cover
        print("job-queue extra not installed; alerts disabled "
              "(pip install 'python-telegram-bot[job-queue]').")

    print(f"Bot up. mode={bot.exec_config.mode}. Waiting for owner {bot.owner_id}.")
    app.run_polling()


if __name__ == "__main__":  # pragma: no cover
    main()
