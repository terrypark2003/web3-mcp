# Prediction-market arbitrage scanner (Polymarket + Kalshi)

Finds three kinds of edge in prediction markets:

1. **Structural arbitrage on Polymarket** — a market's mutually-exclusive,
   collectively-exhaustive outcomes mispriced vs their guaranteed $1 payout.
2. **Cross-venue arbitrage** — the same event priced differently on Kalshi and
   Polymarket, so buying `YES` on one and `NO` on the other locks a profit.
3. **Positive-EV signals** — a market priced away from a fair-value estimate
   you supply (an opinion, *not* risk-free).

> **Detection by default.** The scanner only tells you *where* an edge is.
> The optional Telegram bot can place Polymarket trades, but only as a
> separate, opt-in, dry-run-by-default step (see the bot section).

## The idea

A complete set of *N* outcomes pays exactly **$1** at resolution (one leg
settles to $1, the rest to $0). Two structural edges follow:

| Kind | Action | Edge | Notes |
|------|--------|------|-------|
| **BUY_SET** | Buy 1 share of every leg at its best ask | `1 − Σ ask` | Risk-free $1 at resolution; capital locked until then (APR reported). |
| **MINT_SELL** | Mint a set for $1, sell every leg at its best bid | `Σ bid − 1` | Instant; restricted to binary markets (standard CTF split/merge). |

A binary market is the 2-leg case (`[Yes, No]`); a negative-risk event group
is the N-leg case (one `Yes` token per candidate). Because the two outcome
tokens of a binary market trade on **separate order books**, `ask(Yes) +
ask(No)` can drift below $1 when the books are stale or crossed — that gap is
the arbitrage.

A live `scan` assembles **both**: every binary market, and every negative-risk
event group (e.g. "Who wins the election?") where the candidates' `Yes` prices
sum below $1. Only negRisk events are grouped, because only they are designed
to be collectively exhaustive — a buy-all-`Yes` basket is a guaranteed $1 only
when one candidate is certain to win.

## Quick start

```bash
# Works offline, right now — runs the detector on bundled synthetic data:
python -m polymarket_arb demo

# Run the tests (standard library only):
python -m unittest discover -s tests

# Live scan (needs network — see "Network access" below):
pip install -r requirements.txt
python -m polymarket_arb scan --min-edge 0.01 --limit 300 --json opps.json

# Dump a live market+book snapshot for offline analysis:
python -m polymarket_arb snapshot --out snap.json

# Size a bankroll across the detected edges ("many small bets"):
python -m polymarket_arb allocate --bankroll 1000 --per-market-cap 0.05

# Cross-venue arbitrage between Kalshi and Polymarket (demo offline; --live for real):
python -m polymarket_arb cross-venue

# Positive-EV signals vs a fair-value estimate (NOT risk-free):
python -m polymarket_arb ev
```

Tunables (`scan`/`demo`): `--min-edge` (min net USDC/set), `--min-size` (min
sets at top of book), `--fee-rate` (taker fee as a fraction of notional).

## Cross-venue arbitrage & positive-EV (Kalshi + Polymarket)

The same real-world event often trades on both **Kalshi** (CFTC-regulated, USD)
and **Polymarket** (crypto). Two extra edges follow:

| Mode | What it finds | Risk-free? |
|------|---------------|------------|
| **`cross-venue`** | Buy `YES` on the cheaper venue and `NO` on the other for `< $1` total → guaranteed $1 at resolution. | Yes — *if both venues resolve identically.* |
| **`ev`** | A market priced away from a **fair-value** estimate you supply → positive expected value on one side. | **No.** An opinion, only as good as your number; any single bet can lose in full. |

Two things make this honest rather than a mirage:

- **Fees are modeled per venue.** Kalshi charges `ceil(0.07 · C · P · (1−P))`
  per fill — near 50¢ that's ~1.75¢/contract, enough to erase most raw gaps.
  The scanner applies it *before* reporting an edge (the demo's Fed pair is
  filtered out for exactly this reason; the BTC pair survives at ~2.3¢/set).
- **Pairs are human-curated, never fuzzy-matched.** Two markets can share a
  headline yet resolve on different sources or cutoffs — which would turn a
  "risk-free" pair into two uncorrelated bets. `fixtures/cross_venue_pairs.json`
  is an explicit registry; a human asserts each pair co-resolves before it's
  traded. **Resolution risk is the whole danger here — read both rulebooks.**

For `ev --live`, supply `--fair-values file.json` (a `{market_id: P(YES)}`
map). The fair-value source is pluggable: a model or data feed can replace the
hand-entered map without touching the detector. Size EV bets with
**fractional Kelly** (`portfolio.kelly_stake`), never flat.

## Sizing: making many small bets actually work

Volume does not *create* an edge — it *reveals* one. Spreading capital over
many bets makes your realized return converge toward the per-bet expected
value (law of large numbers). That helps only if each bet is **+EV**; on a
−EV bet the same math makes losses near-certain. So the job is: find +EV
bets (the scanner does this), then size them so the edge compounds without
risking ruin.

`allocate` operationalizes that for arbitrage opportunities:

- **Per-market cap** (`--per-market-cap`, default 5%) bounds how much rides on
  any single market resolution — the real tail risk in a "risk-free" arb.
- **Depth** caps each stake at what the order book can actually fill.
- **Bankroll / max-deployed** (`--max-deployed`) cap total capital at risk.
- **Min stake** (`--min-stake`) is a fee/gas floor so tiny bets aren't placed.

Capital is spread greedily across the best risk-adjusted edges (instant
`MINT_SELL` first, then `BUY_SET` by APR). Diversification only reduces risk
if the markets resolve **independently** — 100 correlated bets are one bet.

For *directional* bets (a genuine forecasting edge, which the scanner does not
generate), `portfolio.kelly_fraction` / `kelly_stake` give growth-optimal
**fractional-Kelly** sizing — use a fraction like 0.5, which is far more
robust to your probability estimate being slightly wrong.

## Network access

The live `scan`/`snapshot` commands talk to Polymarket's public APIs:

- `gamma-api.polymarket.com` — market metadata and `/events` (candidate groups)
- `clob.polymarket.com` — order books
- `api.elections.kalshi.com` — Kalshi public market data (for `cross-venue`)

In a sandboxed environment with an **egress allowlist** (e.g. Claude Code on
the web), both hosts must be added to the allowlist or the client returns
`403 host_not_allowed`. The `demo` command and the test suite need no network.

## Risks & caveats — read before trusting a number

- **Efficiency / competition.** Obvious complete-set gaps are sniped by bots
  in seconds. Persistent edges live in illiquid markets where you can't size.
- **Costs.** The headline edge is **gross**; the realism layer reports a
  **net** edge after `--fee-rate` and a fixed gas estimate. Set a sane
  `--min-edge`; a 0.5¢ "edge" is noise.
- **Depth & the $1 floor → realism score.** Beyond the top-of-book headline,
  the scanner now *walks the full order book* (`realism.py`) to find how many
  sets survive before the edge erodes (`EXEC`), and checks Polymarket's
  $1-per-order minimum — when the cheapest leg needs more shares than the book
  offers, the arb is flagged `[!$1-floor]` (exactly the OpenAI-IPO false
  positive). All of this folds into a **0–100 confidence score** (`CONF`) that
  also weighs lockup and leg count, and the scanner ranks by it — so real money
  sorts above paper edges instead of the other way around.
- **Capital lockup.** `BUY_SET` ties up capital until resolution. Judge it on
  the reported **APR** and confidence score, not the raw edge %. `MINT_SELL`
  is instant, so it scores higher for the same edge.
- **Resolution risk.** The "risk-free $1" assumes clean resolution. Ambiguous
  criteria or a disputed UMA oracle outcome can break the assumption.
- **Exhaustiveness (negRisk).** Multi-leg `BUY_SET` is only truly risk-free if
  the legs are genuinely exhaustive (every outcome, including "none/other").
  Verify before trusting a multi-candidate set.
- **Indicative vs executable prices.** Gamma `outcomePrices` are mid/last and
  used only as a cheap pre-filter; the decision always uses the live book.
- **Jurisdiction.** Polymarket geoblocks US persons (CFTC settlement). Ensure
  you are in a permitted jurisdiction and compliant with local law before
  trading.

## Telegram bot & execution (run it yourself)

There is a Telegram bot that scans, sizes, and — in live mode — places trades.
**You run it on your own machine; it is not run from this repo's environment,
and your keys never touch this chat or the repo.**

```bash
pip install -r requirements-bot.txt
cp .env.example .env          # fill in secrets; .env is gitignored
set -a; . ./.env; set +a
python -m polymarket_arb.telegram_bot
```

Commands (owner-only — the bot ignores every chat except `TELEGRAM_OWNER_ID`):
`/scan`, `/cross`, `/ev`, `/allocate <bankroll>`, `/plan <id>`, `/execute <id>`
then `/confirm`, `/cancel`, `/alerts <on|off|status>`, `/status`.

**Proactive alerts.** The bot polls every `ALERT_INTERVAL_SEC` and pushes a
message when a *new* arbitrage appears (deduped; a vanished-then-reappeared
edge re-fires). Filter with `ALERT_MIN_EDGE_PCT`; toggle live with `/alerts`.
This is the dry-run + alerts workflow — it never places an order on its own.

**Signals channel (broadcaster).** Set `SIGNAL_CHANNEL_ID` and the bot also
broadcasts newly-appeared **risk-free** edges (Polymarket structural + the
cross-venue arbs) to that channel on the same interval — a Polytrage-style
feed. Positive-EV is deliberately kept *out* of the auto-feed (it's an opinion)
and stays on-demand via `/ev`. Set `FAIR_VALUES_FILE` to enable `/ev` live.

**Safety model — read before going live:**

- **Dry-run by default.** Nothing is placed unless `EXECUTION_MODE=live` *and*
  all credentials are present *and* you `/confirm` each trade. Start at
  `MAX_STAKE_USDC=1` for a functional test.
- **Secrets via env only.** API key/secret/passphrase, wallet key, bot token,
  and owner id come from environment variables. Never commit `.env`. Consider a
  dedicated wallet funded with only what you intend to trade.
- **Only `BUY_SET` is executable.** `MINT_SELL` (needs an on-chain CTF split)
  is out of scope for now.
- **Partial-fill risk is real.** Two CLOB orders can't fill atomically; the
  executor places each leg fill-or-kill and tries to unwind if one fails — an
  unwind can slip. Watch your first live fills by hand.
- **Live placement is UNTESTED from here.** The `py-clob-client` calls are
  written to the documented interface but could not be run against the live API
  in this environment. **Validate against your installed client version and a
  $1 trade before trusting larger size.** `signature_type`/`funder` depend on
  whether you trade from an EOA or a Polymarket proxy wallet.

## Web dashboard (browser UI with execution)

A FastAPI dashboard shows all three feeds in the browser with auto-refresh and
lets you place Polymarket `BUY_SET` trades through the **same** stage→confirm,
dry-run-by-default flow as the bot. **You run it on your own machine.**

```bash
pip install -r requirements-web.txt    # plus requirements-bot.txt for live trading
# Try it offline first, no keys or network — bundled demo data:
DASHBOARD_TOKEN=test WEB_DEMO=1 python -m polymarket_arb.webapp
# -> open http://127.0.0.1:8000 and paste the token to connect

# Real data / trading: fill .env (DASHBOARD_TOKEN + the execution secrets), then:
set -a; . ./.env; set +a
python -m polymarket_arb.webapp
```

The browser stages a trade → a modal shows the **exact** order plan and the
dry-run preview → you Confirm. In live mode the modal turns red and says
"PLACE LIVE"; nothing is placed in a single click.

**Security model — this UI can move real money:**

- **Token on every request.** All `/api/*` calls require `X-Dashboard-Token` to
  match `DASHBOARD_TOKEN`; without that env var the service refuses everything.
- **Localhost by default.** Binds to `127.0.0.1`. `WEB_HOST=0.0.0.0` exposes a
  trading UI to your network — only with a strong token and trusted access, 
  ideally behind a VPN/reverse-proxy with TLS. There is no multi-user auth.
- **Dry-run by default.** Live placement needs `EXECUTION_MODE=live` + full
  credentials *and* a per-trade confirm, exactly like the bot. Start at
  `MAX_STAKE_USDC=1`.
- **Same execution caveats** as the bot apply (only `BUY_SET`, non-atomic
  fills, `py-clob-client` calls unrun from this repo — validate with $1 first).

The dashboard logic lives in `web_core.py` (pure, unit-tested); `webapp.py` is
just the HTTP/auth wiring and is best validated by running it locally.

### Public monitor on Vercel (read-only, no keys)

A **read-only** version deploys to Vercel — it shows the three feeds but has
**no wallet, no execution endpoints**, so it is safe to expose publicly.
**Never** deploy the live execution app (the one above) to a public URL: it
would put a wallet private key behind a single shared token on the open
internet. Trading stays on your own machine.

```
api/opportunities.py   Vercel serverless function -> read_only_payload() as JSON
public/index.html      static read-only dashboard (no trade buttons)
vercel.json            bundles the polymarket_arb package + fixtures into the fn
```

Deploy: import the repo at [vercel.com/new](https://vercel.com/new) (or
`npx vercel`). It works immediately on **bundled demo data**. To show live
data, set `LIVE_SCAN=1` in the Vercel project's env vars — the function then
does a *bounded* scan (`LIVE_SCAN_LIMIT`, default 120 markets) to fit the
serverless time budget, and falls back to demo data (flagged in `meta`) if the
scan fails or times out. The serverless model also means the live structural
scan is capped and best-effort; the local app is the source of truth.

## World Cup value betting (Polymarket vs bookmaker consensus)

`python -m polymarket_arb world-cup` (offline demo) finds **positive-EV** World
Cup bets: it averages the implied probabilities across many bookmakers,
de-vigs them to a fair probability, and flags any Polymarket outright priced
below that consensus. This is **not arbitrage** — it's an opinion that the
bookmaker consensus is closer to the truth than Polymarket's price; any single
bet can lose in full, so size with fractional Kelly.

- Live: `world-cup --live` (needs `ODDS_API_KEY` from
  [the-odds-api.com](https://the-odds-api.com/), free ~500 req/month).

There are **two** flavors of value:

- **Outright winner** (the `world-cup` CLI command above) — tournament-long,
  settles only at the final.
- **Per-match** (the alerts) — each game's home/draw/away odds de-vigged into a
  fair probability, compared to the Polymarket match market. These **settle
  right after the match**, so they're the near-dated bets that pair with the
  `NOTIFY_MAX_DAYS` window. This is what "games resolving in a day or two"
  actually means.

The `world-cup-alerts.yml` workflow runs `NOTIFY_MODE=world_cup` every 2 hours
(~360 calls/month, under the free quota) and pushes new **per-match** value bets
to Telegram, each with a tappable link, a `$1 → ~$X 가치` line, and the time to
settlement. Defaults: `NOTIFY_WC_MIN_EDGE=0.05` (a +5% value edge, "$1 worth
~$1.05 at fair odds"; raise to `0.10` for a stricter bar) and `NOTIFY_MAX_DAYS=2`
(only matches in the next two days). For true **live, in-play** odds you need a
paid odds-API plan; then drop the cron to e.g. `*/15 * * * *`. Add `ODDS_API_KEY`
to repo Secrets alongside the Telegram ones.

The value math reuses the de-vig/EV engine (`ev.py` + `sports_value.py`):
`world_cup_match_odds` (h2h) → de-vig per match → compare each team/Draw leg of
the matched Polymarket market to its price.

## Gemini: a plain-language analyst (not the oracle)

Gemini is wired in as an **explanation / query layer over the real numbers** —
it never decides probabilities. (An LLM hallucinates win probabilities; betting
against the market on an invented number loses money. The de-vigged bookmaker
consensus is the benchmark; Gemini only reads it.)

- **`/ask <question>`** in the bot: gathers the current signals (arb, cross-venue,
  EV, World Cup value), passes the real prices + consensus + edges to Gemini, and
  replies in plain language. Gemini is instructed to use only the supplied numbers,
  never invent odds, and flag that it's not risk-free / not advice.
- **Alert enrichment**: set `GEMINI_ENRICH=1` and a one-line Gemini context note is
  appended to alert messages (best-effort; never blocks the alert).

Needs `GEMINI_API_KEY` (Google AI Studio); `GEMINI_MODEL` defaults to
`gemini-2.0-flash`. The prompt builders in `gemini.py` are tested; the REST call
is unrun here — validate with your key.

## Scheduled Telegram alerts (no server to keep on)

`polymarket_arb.notify` is a one-shot scan-and-send pass: it scans, diffs
against the previous run, and pushes only *new* risk-free edges to a Telegram
chat via the Bot API (a plain HTTPS POST — no extra dependency). A bundled
GitHub Action (`.github/workflows/telegram-alerts.yml`) runs it on a schedule,
so you get alerts in the cloud without keeping a machine on.

Setup:

1. Create a bot with **@BotFather**, copy the token.
2. Get your numeric chat id: message your bot, then open
   `https://api.telegram.org/bot<token>/getUpdates` and read `chat.id`
   (or message **@userinfobot**).
3. In the repo: **Settings → Secrets and variables → Actions** → add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **The schedule only runs when the workflow is on the default branch** — merge
   to `main` to activate it. Use the *Run workflow* button (with the **demo**
   box ticked) to send a labelled test message from any branch first.

Dedup state is persisted between runs via the Actions cache, so you aren't
re-pinged about the same edge; a vanished-then-reappeared edge re-fires. EV is
off by default (`NOTIFY_INCLUDE_EV=1` to include it). The notifier never sends
demo data as a real alert — if a live scan fails it sends nothing.

Filters (env vars on the workflow step):
- `NOTIFY_MIN_EDGE_PCT` — minimum edge percent.
- `NOTIFY_MIN_CONFIDENCE` — minimum realism score (0–100); drops paper edges.
- `NOTIFY_MAX_DAYS` — only alert on ops resolving within this many days, so you
  focus on **near-dated** edges where capital isn't locked up (default `2` in
  the bundled workflows). Instant `MINT_SELL` always passes; held ops with an
  unknown resolution date are dropped while this window is active.

## Layout

```
polymarket_arb/
  models.py          Normalized dataclasses (Level, Leg, CompleteSet, Opportunity)
  detect.py          Pure Polymarket detection math + FeeModel  (no network)
  normalize.py       Raw Gamma/CLOB JSON -> models   (no network)
  client.py          Public read-only Polymarket HTTP client (requests)
  scanner.py         Fetch -> prefilter -> detect -> rank -> report
  venues.py          Per-venue fee models (Kalshi schedule, Polymarket)  (no network)
  crossvenue.py      Cross-venue arbitrage detection (no network)
  ev.py              Positive-EV finder vs a pluggable fair-value source (no network)
  kalshi_normalize.py Raw Kalshi JSON -> models   (no network)
  kalshi_client.py   Public read-only Kalshi HTTP client (requests)
  matching.py        Curated cross-venue pair registry (no network)
  multivenue.py      Live cross-venue + EV orchestration
  portfolio.py       Bankroll allocation + fractional-Kelly sizing (no network)
  execution.py       Order-plan building + py-clob-client executor (live = opt-in)
  bot_core.py        Telegram command logic + alerts + broadcaster (pure, testable)
  telegram_bot.py    Thin async Telegram shell (run on your machine)
  notify.py          One-shot Telegram notifier for cron/CI (pure dedup + send)
  web_core.py        Dashboard service: auth + stage/confirm (pure, testable)
  webapp.py          Thin FastAPI shell + static serving (run on your machine)
  web/index.html     Single-file dashboard UI (vanilla JS, no build step)
  demo.py            Loads the bundled synthetic fixtures
  cli.py             argparse CLI: demo / scan / allocate / cross-venue / ev / snapshot
tests/               unittest suite (stdlib only)
```
