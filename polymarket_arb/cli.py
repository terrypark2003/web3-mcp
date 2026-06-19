"""Command-line interface for the Polymarket arbitrage scanner.

Subcommands
-----------
demo      Run the detector on bundled synthetic data (works fully offline).
scan      Fetch live Polymarket data and report arbitrage opportunities.
snapshot  Dump live markets + order books to a JSON file for offline analysis.

``scan`` and ``snapshot`` need network access to gamma-api.polymarket.com and
clob.polymarket.com. ``demo`` does not.
"""

from __future__ import annotations

import argparse
import sys

from .detect import FeeModel, scan_sets
from .demo import load_demo_sets
from .portfolio import SizingConfig, allocate_portfolio, format_portfolio
from .scanner import format_table, scan_live, write_json


def _fee_model(args) -> FeeModel:
    return FeeModel(
        taker_fee_rate=args.fee_rate,
        min_edge_per_set=args.min_edge,
        min_size=args.min_size,
    )


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--min-edge", type=float, default=0.005,
                   help="Minimum net USDC profit per set (default: 0.005)")
    p.add_argument("--min-size", type=float, default=1.0,
                   help="Minimum sets available at top of book (default: 1)")
    p.add_argument("--fee-rate", type=float, default=0.0,
                   help="Taker fee as a fraction of notional (default: 0.0)")
    p.add_argument("--json", dest="json_path", default=None,
                   help="Also write opportunities to this JSON file")


def cmd_demo(args) -> int:
    fees = _fee_model(args)
    opportunities = scan_sets(load_demo_sets(), fees)
    print(format_table(opportunities))
    if args.json_path:
        write_json(opportunities, args.json_path)
        print(f"\nWrote {len(opportunities)} opportunities to {args.json_path}")
    return 0


def cmd_scan(args) -> int:
    from .client import PolymarketClient  # imported here so `demo` needs no `requests`

    fees = _fee_model(args)
    client = PolymarketClient()
    try:
        opportunities = scan_live(client, fees, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 - surface network/egress errors plainly
        print(f"Live scan failed: {exc}", file=sys.stderr)
        print(
            "If this is a sandboxed environment, ensure gamma-api.polymarket.com "
            "and clob.polymarket.com are on the egress allowlist.",
            file=sys.stderr,
        )
        return 1
    print(format_table(opportunities))
    if args.json_path:
        write_json(opportunities, args.json_path)
        print(f"\nWrote {len(opportunities)} opportunities to {args.json_path}")
    return 0


def cmd_allocate(args) -> int:
    fees = _fee_model(args)
    if args.live:
        from .client import PolymarketClient

        client = PolymarketClient()
        try:
            opportunities = scan_live(client, fees, limit=args.limit)
        except Exception as exc:  # noqa: BLE001
            print(f"Live scan failed: {exc}", file=sys.stderr)
            print(
                "Ensure gamma-api.polymarket.com and clob.polymarket.com are on "
                "the egress allowlist.",
                file=sys.stderr,
            )
            return 1
    else:
        opportunities = scan_sets(load_demo_sets(), fees)

    cfg = SizingConfig(
        bankroll=args.bankroll,
        per_market_cap_frac=args.per_market_cap,
        min_stake=args.min_stake,
        max_deployed_frac=args.max_deployed,
    )
    summary = allocate_portfolio(opportunities, cfg)
    print(format_portfolio(summary))
    return 0


def cmd_snapshot(args) -> int:
    import json
    from .client import PolymarketClient
    from .normalize import _maybe_json_list

    client = PolymarketClient()
    markets = client.active_markets()
    token_ids = []
    for market in markets:
        token_ids.extend(str(t) for t in _maybe_json_list(market.get("clobTokenIds")))
    books = client.order_books(token_ids)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"markets": markets, "books": books}, fh)
    print(f"Wrote snapshot: {len(markets)} markets, {len(books)} books -> {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket_arb",
        description="Detect complete-set arbitrage on Polymarket (detection only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Run on bundled synthetic data (offline)")
    _add_common(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    p_scan = sub.add_parser("scan", help="Fetch live data and report opportunities")
    _add_common(p_scan)
    p_scan.add_argument("--limit", type=int, default=None,
                        help="Cap the number of markets scanned (after pre-filter)")
    p_scan.set_defaults(func=cmd_scan)

    p_alloc = sub.add_parser(
        "allocate", help="Size bankroll across detected arbitrage opportunities"
    )
    _add_common(p_alloc)
    p_alloc.add_argument("--bankroll", type=float, required=True,
                         help="Total capital available (USDC)")
    p_alloc.add_argument("--per-market-cap", type=float, default=0.05,
                         help="Max fraction of bankroll on any one market (default: 0.05)")
    p_alloc.add_argument("--min-stake", type=float, default=1.0,
                         help="Fee/gas floor: skip bets smaller than this (USDC, default: 1)")
    p_alloc.add_argument("--max-deployed", type=float, default=1.0,
                         help="Cap on total fraction of bankroll deployed (default: 1.0)")
    p_alloc.add_argument("--live", action="store_true",
                         help="Use live Polymarket data instead of the demo fixture")
    p_alloc.add_argument("--limit", type=int, default=None,
                         help="Cap markets scanned when --live (after pre-filter)")
    p_alloc.set_defaults(func=cmd_allocate)

    p_snap = sub.add_parser("snapshot", help="Dump live markets + books to JSON")
    p_snap.add_argument("--out", default="polymarket_snapshot.json",
                        help="Output path (default: polymarket_snapshot.json)")
    p_snap.set_defaults(func=cmd_snapshot)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
