from decimal import Decimal

import pytest

from sp1execution.recovery.t212_demo_adapter_v01 import (
    BROKER_POST_AUTHORIZED,
    LIVE_EXECUTION_AUTHORIZED,
    RECOVERY_ISIN,
    RECOVERY_TICKER,
    DemoAdapterError,
    normalize_account_summary,
    normalize_positions,
    require_demo_strategy_inventory,
    resolve_recovery_instrument,
)


def instruments():
    return [
        {
            "isin": "IE00BMC38736",
            "ticker": "SMHm_EQ",
            "currencyCode": "EUR",
            "type": "ETF",
            "name": "VanEck Semiconductor (Acc)",
        },
        {
            "isin": "IE00BMC38736",
            "ticker": "SMGBl_EQ",
            "currencyCode": "GBP",
            "type": "ETF",
            "name": "VanEck Semiconductor (Acc)",
        },
        {
            "isin": "IE00BMC38736",
            "ticker": "SMHl_EQ",
            "currencyCode": "USD",
            "type": "ETF",
            "name": "VanEck Semiconductor (Acc)",
        },
    ]


def positions():
    return [
        {
            "instrument": {
                "ticker": "NVDA_US_EQ",
                "isin": "US67066G1040",
            },
            "quantity": 25.0,
            "quantityAvailableForTrading": 25.0,
            "quantityInPies": 0,
            "currentPrice": 100,
            "averagePricePaid": 80,
            "walletImpact": {},
        },
        {
            "instrument": {
                "ticker": "AAPL_US_EQ",
                "isin": "US0378331005",
            },
            "quantity": 50.0,
            "quantityAvailableForTrading": 50.0,
            "quantityInPies": 0,
            "currentPrice": 200,
            "averagePricePaid": 150,
            "walletImpact": {},
        },
    ]


def test_account_summary_synthetic_eur_snapshot():
    x = normalize_account_summary({
        "currency": "EUR",
        "cash": {
            "availableToTrade": 40000.00,
            "reservedForOrders": 0,
            "inPies": 0,
        },
    })

    assert x.currency == "EUR"
    assert (
        x.available_to_trade_eur
        ==
        Decimal("40000.00")
    )

    assert x.broker_post_authorized is False
    assert x.live_execution_authorized is False


def test_wrong_account_currency_fails_closed():
    with pytest.raises(
        DemoAdapterError,
        match="expected account currency EUR",
    ):
        normalize_account_summary({
            "currency": "USD",
            "cash": {
                "availableToTrade": 1000,
            },
        })


def test_exact_recovery_mapping_is_frozen():
    x = resolve_recovery_instrument(
        instruments()
    )

    assert x.isin == RECOVERY_ISIN
    assert x.ticker == "SMHm_EQ"
    assert x.currency == "EUR"
    assert x.instrument_type == "ETF"


def test_ambiguous_eur_mapping_fails_closed():
    rows = instruments()

    rows.append({
        "isin": "IE00BMC38736",
        "ticker": "OTHER_EQ",
        "currencyCode": "EUR",
        "type": "ETF",
        "name": "Unexpected duplicate",
    })

    with pytest.raises(
        DemoAdapterError,
    ):
        resolve_recovery_instrument(
            rows
        )


def test_demo_sp2_inventory_normalizes():
    ps = normalize_positions(
        positions()
    )

    by = require_demo_strategy_inventory(
        ps
    )

    assert set(by) == {
        "AAPL_US_EQ",
        "NVDA_US_EQ",
    }

    assert (
        by["NVDA_US_EQ"].quantity
        ==
        Decimal("25.0")
    )

    assert (
        by["AAPL_US_EQ"].quantity
        ==
        Decimal("50.0")
    )

    assert all(
        p.fully_available_for_trading
        for p in by.values()
    )


def test_position_in_pie_fails_inventory_guard():
    rows = positions()

    rows[0][
        "quantityAvailableForTrading"
    ] = 20

    rows[0][
        "quantityInPies"
    ] = 5.0

    ps = normalize_positions(
        rows
    )

    with pytest.raises(
        DemoAdapterError,
        match="not fully available",
    ):
        require_demo_strategy_inventory(
            ps
        )


def test_unrelated_position_fails_closed():
    rows = positions()

    rows.append({
        "instrument": {
            "ticker": "TSLA_US_EQ",
            "isin": "US88160R1014",
        },
        "quantity": 1,
        "quantityAvailableForTrading": 1,
        "quantityInPies": 0,
    })

    ps = normalize_positions(
        rows
    )

    with pytest.raises(
        DemoAdapterError,
        match="unrelated Demo positions",
    ):
        require_demo_strategy_inventory(
            ps
        )


def test_recovery_position_is_allowed():
    rows = positions()

    rows.append({
        "instrument": {
            "ticker": "SMHm_EQ",
            "isin": "IE00BMC38736",
        },
        "quantity": 2.5,
        "quantityAvailableForTrading": 2.5,
        "quantityInPies": 0,
    })

    ps = normalize_positions(
        rows
    )

    by = require_demo_strategy_inventory(
        ps
    )

    assert RECOVERY_TICKER in by


def test_global_authorization_guards():
    assert LIVE_EXECUTION_AUTHORIZED is False
    assert BROKER_POST_AUTHORIZED is False
