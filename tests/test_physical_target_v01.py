from decimal import Decimal

import pytest

from sp1execution.recovery.physical_target_v01 import (
    PhysicalTargetError,
    construct_physical_target,
)


def as_dict(rows):
    return dict(rows)


def test_reserve_only_entry_requires_no_sp2_sale():
    plan = construct_physical_target(
        target_recovery_weight="0.05",
        sp2_holdings_eur={
            "AAPL_US_EQ": "5400",
            "NVDA_US_EQ": "3600",
        },
        recovery_value_eur="0",
        reserve_available_eur="1000",
    )

    assert plan.total_strategy_nav_eur == Decimal("10000")
    assert plan.target_recovery_eur == Decimal("500")
    assert plan.reserve_used_eur == Decimal("500")
    assert plan.reserve_after_eur == Decimal("500")
    assert plan.sp2_sale_total_eur == Decimal("0")
    assert plan.recovery_buy_eur == Decimal("500")

    assert plan.requires_sell_fill_before_buy is False
    assert (
        plan.requires_sale_proceeds_settlement_confirmation
        is False
    )

    assert plan.funding_ready_without_sale_proceeds is True
    assert plan.broker_post_authorized is False
    assert plan.live_execution_authorized is False


def test_reserve_first_then_proportional_sp2_sale():
    plan = construct_physical_target(
        target_recovery_weight="0.30",
        sp2_holdings_eur={
            "AAPL_US_EQ": "6000",
            "NVDA_US_EQ": "4000",
        },
        recovery_value_eur="0",
        reserve_available_eur="500",
    )

    # Total NAV = 10,500; 30% = 3,150.
    # Reserve funds 500, SP2 funds 2,650.
    assert plan.target_recovery_eur == Decimal("3150")
    assert plan.reserve_used_eur == Decimal("500")
    assert plan.sp2_sale_total_eur == Decimal("2650")

    sales = as_dict(
        plan.sp2_sales_eur
    )

    assert sales["AAPL_US_EQ"] == Decimal("1590.0")
    assert sales["NVDA_US_EQ"] == Decimal("1060.0")

    assert sum(
        sales.values(),
        Decimal("0"),
    ) == Decimal("2650")

    assert plan.recovery_buy_eur == Decimal("3150")

    assert plan.requires_sell_fill_before_buy is True
    assert (
        plan.requires_sale_proceeds_settlement_confirmation
        is True
    )

    assert plan.funding_ready_without_sale_proceeds is False


def test_scale_up_does_not_double_count_existing_recovery():
    plan = construct_physical_target(
        target_recovery_weight="0.60",
        sp2_holdings_eur={
            "AAPL_US_EQ": "4200",
            "NVDA_US_EQ": "2800",
        },
        recovery_value_eur="2000",
        reserve_available_eur="1000",
    )

    assert plan.total_strategy_nav_eur == Decimal("10000")
    assert plan.target_recovery_eur == Decimal("6000")

    # Existing recovery = 2,000, so only 4,000 additional.
    assert plan.recovery_buy_eur == Decimal("4000")

    # 1,000 reserve then 3,000 SP2 sale.
    assert plan.reserve_used_eur == Decimal("1000")
    assert plan.sp2_sale_total_eur == Decimal("3000")

    sales = as_dict(
        plan.sp2_sales_eur
    )

    assert sales["AAPL_US_EQ"] == Decimal("1800.0")
    assert sales["NVDA_US_EQ"] == Decimal("1200.0")


def test_full_recovery_target_uses_all_reserve_then_all_sp2():
    plan = construct_physical_target(
        target_recovery_weight="1.0",
        sp2_holdings_eur={
            "AAPL_US_EQ": "5400",
            "NVDA_US_EQ": "3600",
        },
        recovery_value_eur="0",
        reserve_available_eur="1000",
    )

    assert plan.total_strategy_nav_eur == Decimal("10000")
    assert plan.target_recovery_eur == Decimal("10000")

    assert plan.reserve_used_eur == Decimal("1000")
    assert plan.reserve_after_eur == Decimal("0")

    assert plan.sp2_sale_total_eur == Decimal("9000")
    assert plan.recovery_buy_eur == Decimal("10000")

    assert sum(
        as_dict(
            plan.sp2_sales_eur
        ).values(),
        Decimal("0"),
    ) == Decimal("9000")


def test_no_change_emits_no_trade_plan():
    plan = construct_physical_target(
        target_recovery_weight="0.20",
        sp2_holdings_eur={
            "AAPL_US_EQ": "4200",
            "NVDA_US_EQ": "2800",
        },
        recovery_value_eur="2000",
        reserve_available_eur="1000",
    )

    assert plan.direction == "NO_CHANGE"

    assert plan.sp2_sales_eur == ()
    assert plan.sp2_buys_eur == ()

    assert plan.recovery_buy_eur == Decimal("0")
    assert plan.recovery_sell_eur == Decimal("0")

    assert plan.reserve_after_eur == Decimal("1000")


def test_recovery_exit_requires_current_causal_sp2_weights():
    with pytest.raises(
        PhysicalTargetError,
        match="current causal SP2 return weights",
    ):
        construct_physical_target(
            target_recovery_weight="0",
            sp2_holdings_eur={},
            recovery_value_eur="10000",
            reserve_available_eur="0",
        )


def test_recovery_exit_uses_explicit_non_50_50_causal_weights():
    plan = construct_physical_target(
        target_recovery_weight="0",
        sp2_holdings_eur={},
        recovery_value_eur="10000",
        reserve_available_eur="0",
        sp2_return_weights={
            "AAPL_US_EQ": "0.73",
            "NVDA_US_EQ": "0.27",
        },
    )

    assert plan.direction == "DECREASE_RECOVERY"

    assert plan.recovery_sell_eur == Decimal("10000")
    assert plan.sp2_buy_total_eur == Decimal("10000")

    buys = as_dict(
        plan.sp2_buys_eur
    )

    assert buys["AAPL_US_EQ"] == Decimal("7300.00")
    assert buys["NVDA_US_EQ"] == Decimal("2700.00")

    # Explicit regression: no hidden 50/50 fallback.
    assert buys["AAPL_US_EQ"] != Decimal("5000")
    assert buys["NVDA_US_EQ"] != Decimal("5000")


def test_recovery_reduction_preserves_untouched_reserve():
    plan = construct_physical_target(
        target_recovery_weight="0.30",
        sp2_holdings_eur={
            "AAPL_US_EQ": "2500",
            "NVDA_US_EQ": "1500",
        },
        recovery_value_eur="5000",
        reserve_available_eur="1000",
        sp2_return_weights={
            "AAPL_US_EQ": "0.625",
            "NVDA_US_EQ": "0.375",
        },
    )

    # Total NAV 10k -> target recovery 3k.
    assert plan.target_recovery_eur == Decimal("3000")
    assert plan.recovery_sell_eur == Decimal("2000")

    # H378/reduction proceeds return to SP2.
    # Reserve is untouched.
    assert plan.reserve_used_eur == Decimal("0")
    assert plan.reserve_after_eur == Decimal("1000")

    buys = as_dict(
        plan.sp2_buys_eur
    )

    assert buys["AAPL_US_EQ"] == Decimal("1250.000")
    assert buys["NVDA_US_EQ"] == Decimal("750.000")


def test_recovery_sale_funded_sp2_buy_is_fail_closed_on_settlement():
    plan = construct_physical_target(
        target_recovery_weight="0",
        sp2_holdings_eur={},
        recovery_value_eur="10000",
        reserve_available_eur="0",
        sp2_return_weights={
            "AAPL_US_EQ": "0.6",
            "NVDA_US_EQ": "0.4",
        },
    )

    assert plan.requires_sell_fill_before_buy is True

    assert (
        plan.requires_sale_proceeds_settlement_confirmation
        is True
    )

    assert plan.funding_ready_without_sale_proceeds is False


def test_invalid_target_weight_fails_closed():
    with pytest.raises(
        PhysicalTargetError,
        match=r"\[0, 1\]",
    ):
        construct_physical_target(
            target_recovery_weight="1.01",
            sp2_holdings_eur={
                "AAPL_US_EQ": "10000",
            },
            recovery_value_eur="0",
            reserve_available_eur="0",
        )


def test_negative_values_fail_closed():
    with pytest.raises(
        PhysicalTargetError,
        match="negative reserve",
    ):
        construct_physical_target(
            target_recovery_weight="0.1",
            sp2_holdings_eur={
                "AAPL_US_EQ": "10000",
            },
            recovery_value_eur="0",
            reserve_available_eur="-1",
        )


def test_bad_return_weights_fail_closed():
    with pytest.raises(
        PhysicalTargetError,
        match="must sum to 1",
    ):
        construct_physical_target(
            target_recovery_weight="0",
            sp2_holdings_eur={},
            recovery_value_eur="10000",
            reserve_available_eur="0",
            sp2_return_weights={
                "AAPL_US_EQ": "0.8",
                "NVDA_US_EQ": "0.3",
            },
        )


def test_identity_and_authorization_guards_are_frozen():
    plan = construct_physical_target(
        target_recovery_weight="0.1",
        sp2_holdings_eur={
            "AAPL_US_EQ": "9000",
        },
        recovery_value_eur="0",
        reserve_available_eur="1000",
    )

    assert (
        plan.recovery_instrument_isin
        ==
        "IE00BMC38736"
    )

    assert plan.research_proxy == "SOXX"

    assert plan.nominal_pre_execution_notional is True
    assert plan.broker_post_authorized is False
    assert plan.live_execution_authorized is False
