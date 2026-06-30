# Always-on interactive Telegram bot (favorites "$1 매수" buttons + alerts).
# Runs as a worker — it long-polls Telegram, so there is no inbound port.
# Works on Railway, Fly.io, Render, or any Docker host. Configure secrets in the
# platform (NOT in the image): see DEPLOY.md.
FROM python:3.12-slim

WORKDIR /app

# Base (requests) + bot/live-execution deps (python-telegram-bot[job-queue], py-clob-client).
COPY requirements.txt requirements-bot.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-bot.txt

COPY polymarket_arb ./polymarket_arb

# Defaults to dry-run (places nothing). Set EXECUTION_MODE=live + credentials in
# the platform's secrets to enable real $1 buys.
CMD ["python", "-m", "polymarket_arb.telegram_bot"]
