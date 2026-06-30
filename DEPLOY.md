# Deploying the Telegram bot (always-on "$1 매수" buttons)

The GitHub Actions cron only *sends* alerts — it can't receive a button tap. To
turn the **$1 매수** buttons on you need the interactive bot
(`telegram_bot.py`) running as an always-on worker. It long-polls Telegram, so
there's **no inbound port / domain** — just a process that stays up.

This repo ships a `Dockerfile` (used by both platforms below), a `Procfile`, and
a starter `fly.toml`.

> ⚠️ Real money. The bot defaults to **dry-run** (places nothing). Turn live on
> only after you've watched a dry-run tap. Use a **dedicated wallet funded with
> only what you intend to trade**. Never commit secrets — set them in the
> platform's secrets/variables UI. The live order path (`py-clob-client`) is
> written to its documented interface but is not exercised by the test suite, so
> watch your first live fill by hand.

---

## Option A — Railway (easiest, no CLI)

1. Make sure this repo is on your GitHub (it is).
2. Go to **railway.app → New Project → Deploy from GitHub repo** → pick
   `web3-mcp`. Railway detects the `Dockerfile` and builds the bot.
3. Open the service → **Variables** → add these (start in **dry-run**):

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `TELEGRAM_OWNER_ID` | your numeric id (@userinfobot) |
   | `EXECUTION_MODE` | `dry-run` |
   | `FAV_BUY_USD` | `1` |
   | `FAV_INTERVAL_SEC` | `900` |
   | `NOTIFY_FAV_MIN_PRICE` | `0.91` |
   | `NOTIFY_FAV_MAX_PRICE` | `0.95` |
   | `NOTIFY_MAX_DAYS` | `2` |

4. Deploy. Within ~15 min the bot posts favorites with a **💵 $1 매수** button.
   Tap one → you should see `DRY-RUN … NO ORDERS PLACED` (nothing is bought).
5. **Go live:** add the credentials below and set `EXECUTION_MODE=live`, then
   redeploy. Fund your Polymarket account with USDC (Polygon) first.

It's a **worker** — ignore any "no domain/port" notice; that's expected.

---

## Option B — Fly.io (CLI)

```bash
# 1. Install flyctl + log in
curl -L https://fly.io/install.sh | sh
fly auth login

# 2. From the repo root (edit `app` in fly.toml to a unique name, or let launch set it)
fly launch --no-deploy --copy-config      # detects the Dockerfile; say no to DB/Redis

# 3. Secrets — dry-run first
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_OWNER_ID=... \
  EXECUTION_MODE=dry-run \
  FAV_BUY_USD=1 FAV_INTERVAL_SEC=900 \
  NOTIFY_FAV_MIN_PRICE=0.91 NOTIFY_FAV_MAX_PRICE=0.95 NOTIFY_MAX_DAYS=2

# 4. Deploy, then tap a button in Telegram and confirm "NO ORDERS PLACED"
fly deploy
fly logs                                    # watch it

# 5. Go live (auto-redeploys on secret change)
fly secrets set EXECUTION_MODE=live MAX_STAKE_USDC=1 \
  POLYMARKET_PRIVATE_KEY=... \
  POLYMARKET_API_KEY=... POLYMARKET_API_SECRET=... POLYMARKET_API_PASSPHRASE=...
```

Keep it to one always-on machine: `fly scale count 1`.

---

## Going live — the credentials

Add these only when you're ready for real orders (Polymarket account funded with
USDC on Polygon):

| Variable | What |
|---|---|
| `EXECUTION_MODE` | `live` |
| `MAX_STAKE_USDC` | `1` (per-trade cap) |
| `POLYMARKET_PRIVATE_KEY` | signing wallet key — **dedicated wallet, small funds** |
| `POLYMARKET_API_KEY` / `_API_SECRET` / `_API_PASSPHRASE` | Polymarket L2 API creds |
| `POLYMARKET_FUNDER` | proxy/funder address (only if using a proxy wallet) |

The L2 API creds are derived from your wallet key via `py-clob-client`
(`create_or_derive_api_creds`). Ask me and I'll add a one-off `make-creds`
script that prints them from `POLYMARKET_PRIVATE_KEY`.

---

## Avoid duplicate alerts

The running bot already sends favorites (with buy buttons) **and** arb alerts.
If you keep the GitHub Actions notifiers on too, you'll get each alert twice. So
once the bot is up, disable the cron workflows you no longer need:
**GitHub → Actions →** open *Near-resolution favorites alerts* (and *Telegram
arbitrage alerts* if you want) **→ ⋯ → Disable workflow**.
