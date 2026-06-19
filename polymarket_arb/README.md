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

- `gamma-api.polymarket.com` — market metadata
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

## Execution roadmap (intentionally not built)

Turning a detection into a fill requires, deliberately and separately:

1. A funded Polygon wallet (USDC) + signing key — **never** committed to a repo.
2. `py-clob-client` for L2-authenticated signed orders.
3. Atomic, both-legs-or-neither execution (a partial fill is naked exposure).
4. Live depth checks, retry/cancel logic, and position/risk accounting.

## Layout

```
polymarket_arb/
  models.py     Normalized dataclasses (Level, Leg, CompleteSet, Opportunity)
  detect.py     Pure detection math + FeeModel  (no network)
  normalize.py  Raw Gamma/CLOB JSON -> models   (no network)
  client.py     Public read-only HTTP client (requests)
  scanner.py    Fetch -> prefilter -> detect -> rank -> report
  portfolio.py  Bankroll allocation + fractional-Kelly sizing (no network)
  demo.py       Loads the bundled synthetic fixture
  cli.py        argparse CLI: demo / scan / snapshot
tests/          unittest suite (stdlib only)
```
