# Polymarket complete-set arbitrage scanner

Finds **structural arbitrage** on Polymarket — cases where a market's
mutually-exclusive, collectively-exhaustive outcomes are mispriced relative
to their guaranteed $1 resolution payout.

> **Detection only.** Nothing here signs transactions, holds keys, or places
> orders. It tells you *where* an edge exists; acting on it is a separate,
> deliberate step (see [Execution roadmap](#execution-roadmap)).

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
```

Tunables (`scan`/`demo`): `--min-edge` (min net USDC/set), `--min-size` (min
sets at top of book), `--fee-rate` (taker fee as a fraction of notional).

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

In a sandboxed environment with an **egress allowlist** (e.g. Claude Code on
the web), both hosts must be added to the allowlist or the client returns
`403 host_not_allowed`. The `demo` command and the test suite need no network.

## Risks & caveats — read before trusting a number

- **Efficiency / competition.** Obvious complete-set gaps are sniped by bots
  in seconds. Persistent edges live in illiquid markets where you can't size.
- **Costs.** Edges are reported **gross** of Polygon gas and slippage. Set
  `--fee-rate` and a sane `--min-edge`; a 0.5¢ "edge" is noise.
- **Depth.** Sizing is the *thinnest leg's top-of-book size* — a conservative
  cap. Real fills walk the book and move the price against you.
- **Capital lockup.** `BUY_SET` ties up capital until resolution. Judge it on
  the reported **APR**, not the raw edge %.
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
`/scan`, `/allocate <bankroll>`, `/plan <id>`, `/execute <id>` then `/confirm`,
`/cancel`, `/status`.

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

## Layout

```
polymarket_arb/
  models.py       Normalized dataclasses (Level, Leg, CompleteSet, Opportunity)
  detect.py       Pure detection math + FeeModel  (no network)
  normalize.py    Raw Gamma/CLOB JSON -> models   (no network)
  client.py       Public read-only HTTP client (requests)
  scanner.py      Fetch -> prefilter -> detect -> rank -> report
  portfolio.py    Bankroll allocation + fractional-Kelly sizing (no network)
  execution.py    Order-plan building + py-clob-client executor (live = opt-in)
  bot_core.py     Telegram command logic (pure, testable)
  telegram_bot.py Thin async Telegram shell (run on your machine)
  demo.py         Loads the bundled synthetic fixture
  cli.py          argparse CLI: demo / scan / allocate / snapshot
tests/            unittest suite (stdlib only)
```
