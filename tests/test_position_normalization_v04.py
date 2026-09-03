from __future__ import annotations

import pytest

from sp1execution.engine.planner import (
    InstrumentQuote,
    make_orders,
    position_quantities,
)
from sp1execution.execution.broker_executor_v04 import (
    _canonical_source_positions,
    _verify_positions,
)

SYNTHETIC_T212_NESTED_POSITIONS = [
    {
        "averagePricePaid": 200.0,
        "createdAt": "2024-01-02T10:00:00+00:00",
        "currentPrice": 200.0,
        "instrument": {
            "currency": "USD",
            "isin": "US67066G1040",
            "name": "Nvidia",
            "ticker": "NVDA_US_EQ",
        },
        "quantity": 25.0,
        "quantityAvailableForTrading": 25.0,
        "quantityInPies": 0,
        "walletImpact": {
            "currency": "EUR",
            "currentValue": 5000.0,
            "fxImpact": 0.0,
            "totalCost": 5000.0,
            "unrealizedProfitLoss": 0.0,
        },
    },
    {
        "averagePricePaid": 100.0,
        "createdAt": "2024-01-02T10:00:00+00:00",
        "currentPrice": 100.0,
        "instrument": {
            "currency": "USD",
            "isin": "US0378331005",
            "name": "Apple",
            "ticker": "AAPL_US_EQ",
        },
        "quantity": 50.0,
        "quantityAvailableForTrading": 50.0,
        "quantityInPies": 0,
        "walletImpact": {
            "currency": "EUR",
            "currentValue": 5000.0,
            "fxImpact": 0.0,
            "totalCost": 5000.0,
            "unrealizedProfitLoss": 0.0,
        },
    },
]



def test_synthetic_trading212_nested_position_payload_is_normalized():
    assert position_quantities(SYNTHETIC_T212_NESTED_POSITIONS) == {
        "NVDA_US_EQ": 25.0,
        "AAPL_US_EQ": 50.0,
    }


def test_legacy_flat_position_payload_remains_supported():
    assert position_quantities(
        [
            {"ticker": "AAPL_US_EQ", "quantity": 1.25},
            {"ticker": "NVDA_US_EQ", "quantity": 2.5},
        ]
    ) == {
        "AAPL_US_EQ": 1.25,
        "NVDA_US_EQ": 2.5,
    }


def test_matching_flat_and_nested_ticker_is_allowed():
    assert position_quantities(
        [
            {
                "ticker": "AAPL_US_EQ",
                "instrument": {"ticker": "AAPL_US_EQ"},
                "quantity": 3.0,
            }
        ]
    ) == {"AAPL_US_EQ": 3.0}


def test_conflicting_flat_and_nested_ticker_fails_closed():
    with pytest.raises(ValueError, match="conflicting ticker"):
        position_quantities(
            [
                {
                    "ticker": "AAPL_US_EQ",
                    "instrument": {"ticker": "NVDA_US_EQ"},
                    "quantity": 1.0,
                }
            ]
        )


def test_position_without_any_ticker_fails_closed():
    with pytest.raises(ValueError, match="missing ticker"):
        position_quantities([{"quantity": 1.0}])


def test_duplicate_position_ticker_fails_closed():
    with pytest.raises(ValueError, match="duplicate ticker"):
        position_quantities(
            [
                {"ticker": "AAPL_US_EQ", "quantity": 1.0},
                {
                    "instrument": {"ticker": "AAPL_US_EQ"},
                    "quantity": 2.0,
                },
            ]
        )


def test_non_numeric_position_quantity_fails_closed():
    with pytest.raises(ValueError, match="invalid quantity"):
        position_quantities(
            [
                {
                    "instrument": {"ticker": "AAPL_US_EQ"},
                    "quantity": "not-a-number",
                }
            ]
        )


def test_make_orders_values_nested_positions_instead_of_treating_them_as_zero():
    positions = [
        {
            "instrument": {"ticker": "AAPL_US_EQ"},
            "quantity": 50.0,
        },
        {
            "instrument": {"ticker": "NVDA_US_EQ"},
            "quantity": 50.0,
        },
    ]
    quotes = {
        "AAPL": InstrumentQuote(
            logical_symbol="AAPL",
            broker_ticker="AAPL_US_EQ",
            price=100.0,
            currency="USD",
        ),
        "NVDA": InstrumentQuote(
            logical_symbol="NVDA",
            broker_ticker="NVDA_US_EQ",
            price=100.0,
            currency="USD",
        ),
    }

    orders, current_values = make_orders(
        nav_eur=10000.0,
        target_weights={"AAPL": 0.5, "NVDA": 0.5},
        quotes=quotes,
        positions=positions,
        eurusd=1.0,
    )

    assert orders == []
    assert current_values == {
        "AAPL": 5000.0,
        "NVDA": 5000.0,
    }


def test_m5b_source_position_capture_uses_nested_payload():
    assert _canonical_source_positions(
        SYNTHETIC_T212_NESTED_POSITIONS,
        strategy_tickers={
            "AAPL_US_EQ",
            "NVDA_US_EQ",
            "VUAAm_EQ",
        },
    ) == {
        "AAPL_US_EQ": 50.0,
        "NVDA_US_EQ": 25.0,
    }


def test_m5b_position_reconciliation_accepts_nested_payload():
    _verify_positions(
        expected={
            "AAPL_US_EQ": 50.0,
            "NVDA_US_EQ": 25.0,
        },
        positions=SYNTHETIC_T212_NESTED_POSITIONS,
    )
