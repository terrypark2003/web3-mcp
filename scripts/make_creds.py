"""Print Polymarket L2 API creds derived from your signing key.

You normally DON'T need this — the bot derives the L2 creds from
``POLYMARKET_PRIVATE_KEY`` automatically at connect time. Use this only if you
want to set the three creds explicitly in your platform's variables, or to
verify your key works.

Usage:
    POLYMARKET_PRIVATE_KEY=0x... python scripts/make_creds.py

The private key is read from the environment and is never printed. Needs network
access to clob.polymarket.com and ``pip install py-clob-client-v2`` (CLOB V2).
"""

import os
import sys


def main() -> int:
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("Set POLYMARKET_PRIVATE_KEY in the environment first.", file=sys.stderr)
        return 1
    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError:
        print("pip install py-clob-client-v2", file=sys.stderr)
        return 1

    host = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
    funder = os.environ.get("POLYMARKET_FUNDER") or None
    client = ClobClient(host=host, key=key, chain_id=137, funder=funder)
    creds = client.create_or_derive_api_key()  # one network call

    print("# Paste these into your platform's variables (optional — the bot also")
    print("# derives them automatically from POLYMARKET_PRIVATE_KEY):")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
