"""Per-venue trading-fee models.

Cross-venue arbitrage only works if the fees are modeled correctly, and the
two venues charge very differently:

* **Polymarket** — the CLOB has historically charged no explicit trading fee;
  the real cost is Polygon gas plus slippage. Modeled as a (default 0) rate on
  notional, optionally plus a flat per-order gas estimate.

* **Kalshi** — charges an explicit trading fee on every fill, per its published
  general fee schedule:

        fee = ceil( 0.07 * C * P * (1 - P) )   rounded UP to the next cent

  where ``C`` is the number of contracts and ``P`` is the fill price in dollars
  (0-1). The fee is maximized near P=0.50 and shrinks toward the tails. This is
  a *taker* schedule; some markets differ, so the rate is configurable. Always
  re-confirm against Kalshi's current schedule before sizing real money.

These fees are what separate a real cross-venue edge from a mirage: a 3¢ raw
gap on a 50¢ contract is almost entirely eaten by Kalshi's ~1.75¢/contract fee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

KALSHI = "kalshi"
POLYMARKET = "polymarket"


@dataclass
class VenueFee:
    """Fee model for one venue. ``fee()`` returns USD cost for a whole order."""

    name: str

    def fee(self, price: float, contracts: float) -> float:  # pragma: no cover - base
        raise NotImplementedError


@dataclass
class FlatRateFee(VenueFee):
    """Rate on traded notional plus an optional flat per-order cost (e.g. gas)."""

    rate: float = 0.0
    flat: float = 0.0

    def fee(self, price: float, contracts: float) -> float:
        if contracts <= 0:
            return 0.0
        return self.rate * price * contracts + self.flat


@dataclass
class KalshiFee(VenueFee):
    """Kalshi general fee schedule: ceil(rate * C * P * (1-P)) to the cent."""

    rate: float = 0.07

    def fee(self, price: float, contracts: float) -> float:
        if contracts <= 0:
            return 0.0
        raw = self.rate * contracts * price * (1.0 - price)
        return math.ceil(raw * 100.0 - 1e-9) / 100.0


def default_venue_fees() -> dict[str, VenueFee]:
    """Sensible defaults: explicit Kalshi schedule, ~free Polymarket."""
    return {
        KALSHI: KalshiFee(name=KALSHI),
        POLYMARKET: FlatRateFee(name=POLYMARKET, rate=0.0, flat=0.0),
    }


def fee_for(venue: str, fees: dict[str, VenueFee]) -> VenueFee:
    """Look up a venue's fee model, defaulting to a free flat-rate model."""
    return fees.get(venue) or FlatRateFee(name=venue, rate=0.0, flat=0.0)
