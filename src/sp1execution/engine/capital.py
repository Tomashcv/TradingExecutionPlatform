from __future__ import annotations

import os

DEFAULT_INITIAL_CAPITAL_EUR = 10_000.0


def initial_capital_eur() -> float:
    raw = os.getenv(
        "SP1_STRATEGY_INITIAL_CAPITAL_EUR",
        str(DEFAULT_INITIAL_CAPITAL_EUR),
    )
    value = float(raw)
    if not 100.0 <= value <= 1_000_000.0:
        raise ValueError(
            "SP1_STRATEGY_INITIAL_CAPITAL_EUR must be between 100 and 1,000,000."
        )
    return value
