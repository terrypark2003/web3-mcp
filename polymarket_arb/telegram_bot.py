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

    return ArbBot(
        owner_id=owner_id,
        scan_fn=scan_fn,
        executor=executor,
        exec_config=config,
        fee_model=fee,
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
    print(f"Bot up. mode={bot.exec_config.mode}. Waiting for owner {bot.owner_id}.")
    app.run_polling()


if __name__ == "__main__":  # pragma: no cover
    main()
