from decimal import Decimal

import pytest

from sp1execution.recovery.demo_physical_plan_v01 import (
    BROKER_POST_AUTHORIZED,
    LIVE_EXECUTION_AUTHORIZED,
    DemoPhysicalPlanError,
    bootstrap_new_operational_reserve_eur,
    build_demo_physical_plan,
)
from sp1execution.recovery.t212_demo_adapter_v01 import (
    normalize_account_summary,
    normalize_positions,
)


def account(available="40000.0"):
    return normalize_account_summary({
        "currency": "EUR",
        "cash": {"availableToTrade": available, "reservedForOrders": 0, "inPies": 0},
    })


def current_positions():
    return normalize_positions([
        {
            "instrument": {"ticker": "NVDA_US_EQ", "isin": "US67066G1040"},
            "quantity": 25.0, "quantityAvailableForTrading": 25.0, "quantityInPies": 0,
            "currentPrice": 200.0, "averagePricePaid": 200.0,
            "walletImpact": {"currency": "EUR", "currentValue": 5000.0, "fxImpact": 0.0, "totalCost": 5000.0, "unrealizedProfitLoss": 0.0},
        },
        {
            "instrument": {"ticker": "AAPL_US_EQ", "isin": "US0378331005"},
            "quantity": 50.0, "quantityAvailableForTrading": 50.0, "quantityInPies": 0,
            "currentPrice": 100.0, "averagePricePaid": 100.0,
            "walletImpact": {"currency": "EUR", "currentValue": 5000.0, "fxImpact": 0.0, "totalCost": 5000.0, "unrealizedProfitLoss": 0.0},
        },
    ])


def test_new_operational_state_reserve_is_zero():
    assert (
        bootstrap_new_operational_reserve_eur()
        ==
        Decimal("0")
    )


def test_current_snapshot_10pct_uses_no_external_broker_cash():
    result = build_demo_physical_plan(
        account=account(),
        positions=current_positions(),
        expected_sp2_tickers={
            "AAPL_US_EQ",
            "NVDA_US_EQ",
        },
        target_recovery_weight="0.10",
        strategy_reserve_eur="0",
    )

    plan = result.physical_target

    assert (
        plan.total_strategy_nav_eur
        ==
        Decimal("10000.0")
    )

    assert (
        plan.target_recovery_eur
        ==
        Decimal("1000.0")
    )

    assert (
        plan.recovery_buy_eur
        ==
        Decimal("1000.0")
    )

    assert (
        plan.reserve_used_eur
        ==
        Decimal("0")
    )

    assert (
        plan.sp2_sale_total_eur
        ==
        Decimal("1000.0")
    )

    sales = dict(
        plan.sp2_sales_eur
    )

    assert (
        sales["AAPL_US_EQ"]
        ==
        Decimal("500.0")
    )

    assert (
        sales["NVDA_US_EQ"]
        ==
        Decimal("500.0")
    )

    assert result.requires_sell_fill_before_buy is True
    assert (
        result.requires_sale_proceeds_settlement_confirmation
        is True
    )

    assert (
        result.funding_feasible_without_sale_proceeds
        is False
    )

    assert result.buy_authorized_now is False
    assert result.broker_cash_used_as_strategy_reserve is False


def test_large_broker_cash_does_not_inflate_strategy_nav():
    result = build_demo_physical_plan(
        account=account("999999"),
        positions=current_positions(),
        expected_sp2_tickers={
            "AAPL_US_EQ",
            "NVDA_US_EQ",
        },
        target_recovery_weight="0.30",
        strategy_reserve_eur="0",
    )

    assert (
        result.physical_target.total_strategy_nav_eur
        ==
        Decimal("10000.0")
    )

    assert (
        result.physical_target.target_recovery_eur
        ==
        Decimal("3000.0")
    )

    assert (
        result.broker_cash_outside_strategy_reserve_eur
        ==
        Decimal("999999")
    )


def test_explicit_strategy_reserve_is_used_before_sp2_sales():
    result = build_demo_physical_plan(
        account=account(),
        positions=current_positions(),
        expected_sp2_tickers=[
            "AAPL_US_EQ",
            "NVDA_US_EQ",
        ],
        target_recovery_weight="0.10",
        strategy_reserve_eur="1000",
    )

    plan = result.physical_target

    assert (
        plan.total_strategy_nav_eur
        ==
        Decimal("11000.0")
    )

    assert (
        plan.target_recovery_eur
        ==
        Decimal("1100.0")
    )

    assert (
        plan.reserve_used_eur
        ==
        Decimal("1000")
    )

    assert (
        plan.sp2_sale_total_eur
        ==
        Decimal("100.0")
    )

    assert (
        result.broker_cash_outside_strategy_reserve_eur
        ==
        Decimal("39000.0")
    )


def test_strategy_reserve_cannot_exceed_broker_available_cash():
    with pytest.raises(
        DemoPhysicalPlanError,
        match="exceeds broker availableToTrade",
    ):
        build_demo_physical_plan(
            account=account("500"),
            positions=current_positions(),
            expected_sp2_tickers={
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            },
            target_recovery_weight="0.10",
            strategy_reserve_eur="501",
        )


def test_wallet_impact_must_be_eur():
    rows = list(
        current_positions()
    )

    bad = rows[0]

    object.__setattr__(
        bad,
        "wallet_impact",
        {
            "currency": "USD",
            "currentValue": 5000.0,
        },
    )

    with pytest.raises(
        DemoPhysicalPlanError,
        match="is not EUR",
    ):
        build_demo_physical_plan(
            account=account(),
            positions=rows,
            expected_sp2_tickers={
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            },
            target_recovery_weight="0.10",
            strategy_reserve_eur="0",
        )


def test_scope_is_dynamic_not_hardcoded_to_aapl_nvda():
    positions = normalize_positions([
        {
            "instrument": {
                "ticker": "MSFT_US_EQ",
                "isin": "US5949181045",
            },
            "quantity": 10,
            "quantityAvailableForTrading": 10,
            "quantityInPies": 0,
            "walletImpact": {
                "currency": "EUR",
                "currentValue": 6000,
            },
        },
        {
            "instrument": {
                "ticker": "GOOGL_US_EQ",
                "isin": "US02079K3059",
            },
            "quantity": 20,
            "quantityAvailableForTrading": 20,
            "quantityInPies": 0,
            "walletImpact": {
                "currency": "EUR",
                "currentValue": 4000,
            },
        },
    ])

    result = build_demo_physical_plan(
        account=account(),
        positions=positions,
        expected_sp2_tickers={
            "MSFT_US_EQ",
            "GOOGL_US_EQ",
        },
        target_recovery_weight="0.10",
        strategy_reserve_eur="0",
    )

    assert result.expected_sp2_tickers == (
        "GOOGL_US_EQ",
        "MSFT_US_EQ",
    )

    assert (
        result.physical_target.total_strategy_nav_eur
        ==
        Decimal("10000")
    )


def test_unrelated_position_fails_closed():
    rows = list(
        current_positions()
    )

    rows.extend(
        normalize_positions([
            {
                "instrument": {
                    "ticker": "TSLA_US_EQ",
                    "isin": "US88160R1014",
                },
                "quantity": 1,
                "quantityAvailableForTrading": 1,
                "quantityInPies": 0,
                "walletImpact": {
                    "currency": "EUR",
                    "currentValue": 100,
                },
            },
        ])
    )

    with pytest.raises(
        DemoPhysicalPlanError,
        match="outside current strategy scope",
    ):
        build_demo_physical_plan(
            account=account(),
            positions=rows,
            expected_sp2_tickers={
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            },
            target_recovery_weight="0.10",
            strategy_reserve_eur="0",
        )


def test_recovery_exit_requires_explicit_current_causal_return_weights():
    rows = list(
        current_positions()
    )

    rows.extend(
        normalize_positions([
            {
                "instrument": {
                    "ticker": "SMHm_EQ",
                    "isin": "IE00BMC38736",
                },
                "quantity": 10,
                "quantityAvailableForTrading": 10,
                "quantityInPies": 0,
                "walletImpact": {
                    "currency": "EUR",
                    "currentValue": 2000,
                },
            },
        ])
    )

    with pytest.raises(
        DemoPhysicalPlanError,
        match="current causal SP2 return weights are required",
    ):
        build_demo_physical_plan(
            account=account(),
            positions=rows,
            expected_sp2_tickers={
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            },
            target_recovery_weight="0",
            strategy_reserve_eur="0",
            sp2_return_weights=None,
        )


def test_recovery_exit_uses_explicit_current_causal_return_weights():
    rows = list(
        current_positions()
    )

    rows.extend(
        normalize_positions([
            {
                "instrument": {
                    "ticker": "SMHm_EQ",
                    "isin": "IE00BMC38736",
                },
                "quantity": 10,
                "quantityAvailableForTrading": 10,
                "quantityInPies": 0,
                "walletImpact": {
                    "currency": "EUR",
                    "currentValue": 2000,
                },
            },
        ])
    )

    result = build_demo_physical_plan(
        account=account(),
        positions=rows,
        expected_sp2_tickers={
            "AAPL_US_EQ",
            "NVDA_US_EQ",
        },
        target_recovery_weight="0",
        strategy_reserve_eur="0",
        sp2_return_weights={
            "AAPL_US_EQ": "0.55",
            "NVDA_US_EQ": "0.45",
        },
    )

    plan = result.physical_target

    assert plan.direction == "DECREASE_RECOVERY"

    assert (
        plan.recovery_sell_eur
        ==
        Decimal("2000")
    )

    assert dict(
        plan.sp2_buys_eur
    ) == {
        "AAPL_US_EQ":
            Decimal("1100.00"),

        "NVDA_US_EQ":
            Decimal("900.00"),
    }

    assert result.requires_sell_fill_before_buy is True
    assert (
        result.requires_sale_proceeds_settlement_confirmation
        is True
    )

    assert result.buy_authorized_now is False


def test_authorization_remains_false():
    assert LIVE_EXECUTION_AUTHORIZED is False
    assert BROKER_POST_AUTHORIZED is False
