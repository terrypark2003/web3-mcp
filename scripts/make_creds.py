"""Print Polymarket L2 API creds derived from your signing key.

You normally DON'T need this — the bot derives the L2 creds from
``POLYMARKET_PRIVATE_KEY`` automatically at connect time. Use this only if you
want to set the three creds explicitly in your platform's variables, or to
verify your key works.

Usage:
    POLYMARKET_PRIVATE_KEY=0x... python scripts/make_creds.py

The private key is read from the environment and is never printed. Needs network
access to clob.polymarket.com and ``pip install polymarket-client`` (the
official unified SDK; see requirements-bot.txt).
"""

import os
import sys


def main() -> int:
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("Set POLYMARKET_PRIVATE_KEY in the environment first.", file=sys.stderr)
        return 1
    try:
        from polymarket import SecureClient
    except ImportError:
        print("pip install polymarket-client", file=sys.stderr)
        return 1

    funder = os.environ.get("POLYMARKET_FUNDER") or None
    # create() derives (or creates) the L2 creds from the key — one network call.
    client = SecureClient.create(private_key=key, wallet=funder)
    creds = client.credentials

    print("# Paste these into your platform's variables (optional — the bot also")
    print("# derives them automatically from POLYMARKET_PRIVATE_KEY):")
    print(f"POLYMARKET_API_KEY={creds.key}")
    print(f"POLYMARKET_API_SECRET={creds.secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.passphrase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
