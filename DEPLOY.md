# Deploying the Telegram bot (always-on "$1 매수" buttons)

The GitHub Actions cron only *sends* alerts — it can't receive a button tap. To
turn the **$1 매수** buttons on you need the interactive bot
(`telegram_bot.py`) running as an always-on worker. It long-polls Telegram, so
there's **no inbound port / domain** — just a process that stays up.

This repo ships a `Dockerfile` (used by both platforms below), a `Procfile`, and
a starter `fly.toml`.

> 🌏 **Pick a region the API can trade from — this decides whether live buys work.**
> Polymarket geo-blocks by the **server's** IP, not yours. Run the bot from a
> blocked region and live orders fail with
> `403 Trading restricted in your region` (dry-run still works — it never calls
> Polymarket). Per the [geoblock list](https://docs.polymarket.com/api-reference/geoblock):
> - **Blocked** (no trading): United States, Australia, Germany, Netherlands,
>   France, UK, Italy, Brazil, … → Fly.io `iad`/`sjc`/`ord`/`syd`/`fra`/`ams`/`cdg`/`lhr`/`gru`
>   and Railway `US`/`EU West (Amsterdam)` are all out.
> - **Close-only** (can *close* a position but not **open** a $1 buy):
>   Singapore, Poland, Thailand, Taiwan → Railway's `Southeast Asia` and Fly.io
>   `sin`/`waw` won't work for buying.
> - **Frontend UI restricted ≠ API blocked**: a few countries (notably **Japan**)
>   are blocked only on Polymarket's *website*; the **CLOB API is open**. This bot
>   trades via the API, so these regions work.
> - **✅ Use Tokyo (`nrt`)**: Japan is frontend-UI-restricted but **API-open**, and
>   it's the closest live Fly region to Korea. Hong Kong (`hkg`) would also be
>   fine but Fly **deprecated** it (can't provision new machines). `nrt` is
>   pinned in `fly.toml`. Railway has no Tokyo/HK region, so for *live* trading
>   use **Fly.io** (Option B); Railway is fine for **dry-run / alerts only**.
> - **Fallback**: Mumbai (`bom`, India) is fully allowed if Tokyo ever changes.

> ⚠️ Real money. The bot defaults to **dry-run** (places nothing). Turn live on
> only after you've watched a dry-run tap. Use a **dedicated wallet funded with
> only what you intend to trade**. Never commit secrets — set them in the
> platform's secrets/variables UI. The live order path (`py-clob-client`) is
> written to its documented interface but is not exercised by the test suite, so
> watch your first live fill by hand.

---

## Option A — Railway (easiest, but dry-run / alerts only)

> ⚠️ Railway has no Polymarket-Allowed region: `US` is **Blocked** and
> `Southeast Asia (Singapore)` is **Close-only**. So Railway is fine for
> **dry-run and alerts**, but **live $1 buys will 403**. For live trading,
> use **Option B — Fly.io (`hkg`)** below.

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

## Option B — Fly.io (recommended for live trading — Tokyo region)

`fly.toml` already pins **`primary_region = "nrt"`** (Tokyo, Japan). Japan is
"frontend UI restricted" on Polymarket but the **CLOB API is open**, and this bot
trades via the API — so live buys go through instead of 403'ing. It's also the
closest live Fly region to Korea. (Hong Kong `hkg` would also be API-allowed but
Fly **deprecated** it; Singapore `sin` is Close-only, so it can't open buys.)

```bash
# 1. Install flyctl + log in
curl -L https://fly.io/install.sh | sh      # Windows: winget install flyio.flyctl
fly auth login

# 2. From the repo root. --copy-config keeps the committed fly.toml (so the nrt
#    region + worker process stick). Edit `app` to a unique name, or let launch set it.
fly launch --no-deploy --copy-config        # detects the Dockerfile; say no to DB/Redis

# 3. Make sure the machine is actually in Tokyo (region = the geoblock check)
fly machine list                            # REGION column should read nrt
# If launch placed it elsewhere, clone to Tokyo and destroy the wrong one:
#   fly machine clone <ID> --region nrt
#   fly machine destroy <ID> --force

# 4. Secrets — dry-run first
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_OWNER_ID=... \
  EXECUTION_MODE=dry-run \
  FAV_BUY_USD=1 FAV_INTERVAL_SEC=900 \
  NOTIFY_FAV_MIN_PRICE=0.91 NOTIFY_FAV_MAX_PRICE=0.95 NOTIFY_MAX_DAYS=2

# 5. Deploy, then tap a button in Telegram and confirm "NO ORDERS PLACED"
fly deploy
fly status                                  # confirm the machine's Region is nrt
fly logs                                    # watch it

# 6. Go live (auto-redeploys on secret change) — only the private key is needed;
#    the L2 API creds derive from it automatically.
fly secrets set EXECUTION_MODE=live MAX_STAKE_USDC=1 POLYMARKET_PRIVATE_KEY=0x...
```

Keep it to one always-on machine **in Tokyo**: `fly scale count 1 --region nrt`.
If a live tap still returns `403 Trading restricted`, run `fly status` — the
machine's Region must read `nrt`. (If Tokyo ever stops working, fall back to
Mumbai `bom` — India is fully allowed: `fly machine clone <ID> --region bom`.)

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
