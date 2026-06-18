"""Load the bundled synthetic complete sets for the offline demo."""

from __future__ import annotations

import json
import os
from typing import Optional

from .models import CompleteSet, Leg, Level

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "demo_sets.json")


def _level(raw: Optional[list]) -> Optional[Level]:
    if not raw:
        return None
    price, size = raw
    return Level(price=float(price), size=float(size))


def load_demo_sets(path: str = _FIXTURE) -> list[CompleteSet]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    sets: list[CompleteSet] = []
    for entry in data.get("sets", []):
        legs = [
            Leg(
                token_id=leg["token_id"],
                outcome=leg["outcome"],
                best_ask=_level(leg.get("ask")),
                best_bid=_level(leg.get("bid")),
            )
            for leg in entry["legs"]
        ]
        sets.append(
            CompleteSet(
                market_id=entry["market_id"],
                question=entry["question"],
                legs=legs,
                neg_risk=entry.get("neg_risk", False),
                exhaustive=entry.get("exhaustive", True),
                end_date=entry.get("end_date"),
            )
        )
    return sets
