"""Polymarket complete-set arbitrage scanner (detection only).

This package finds *structural* arbitrage on Polymarket: cases where the
mutually-exclusive, collectively-exhaustive outcomes of a market can be
bought for less than $1 (guaranteed $1 payout at resolution) or where a
freshly minted complete set can be sold for more than $1.

It is DETECTION ONLY. Nothing in this package signs transactions, holds
keys, or places orders. See README.md for the execution roadmap and the
risk/legal caveats.
"""

__version__ = "0.1.0"
