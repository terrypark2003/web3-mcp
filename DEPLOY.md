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

# 5. Go live (auto-redeploys on secret change) — only the private key is needed;
#    the L2 API creds derive from it automatically.
fly secrets set EXECUTION_MODE=live MAX_STAKE_USDC=1 POLYMARKET_PRIVATE_KEY=0x...
```

Keep it to one always-on machine: `fly scale count 1`.

---

## Going live — the credentials

Add these only when you're ready for real orders (Polymarket account funded with
USDC on Polygon):

| Variable | What | Required? |
|---|---|---|
| `EXECUTION_MODE` | `live` | yes |
| `MAX_STAKE_USDC` | `1` (per-trade cap) | recommended |
| `POLYMARKET_PRIVATE_KEY` | signing wallet key — **dedicated wallet, small funds** | **yes** |
| `POLYMARKET_FUNDER` | proxy/funder address (only if you use a Polymarket proxy wallet) | if proxy |
| `POLYMARKET_API_KEY` / `_API_SECRET` / `_API_PASSPHRASE` | L2 API creds | **no — auto-derived** |

**You only need `POLYMARKET_PRIVATE_KEY`.** The three L2 API creds are *not*
found in any UI — they're derived from the key. The bot does that for you on
connect, so you can leave them blank. (If you'd rather set them explicitly, run
`POLYMARKET_PRIVATE_KEY=0x... python scripts/make_creds.py` once and paste its
output.)

### Where the private key comes from
- **External wallet (MetaMask etc.):** export the private key from the wallet
  (e.g. MetaMask → Account details → Show private key).
- **Polymarket email/Magic account:** Polymarket → wallet/settings → **Export
  private key**.
- **Use a dedicated wallet** funded with only the USDC you intend to trade —
  never your main wallet. Never commit or share the key.

---

## Avoid duplicate alerts

The running bot already sends favorites (with buy buttons) **and** arb alerts.
If you keep the GitHub Actions notifiers on too, you'll get each alert twice. So
once the bot is up, disable the cron workflows you no longer need:
**GitHub → Actions →** open *Near-resolution favorites alerts* (and *Telegram
arbitrage alerts* if you want) **→ ⋯ → Disable workflow**.
