"""
A6B1 — Trading 212 Demo physical-plan integration.

Responsibilities:
- consume broker-normalized positions from A5C;
- obtain EUR valuation only from walletImpact.currentValue;
- scope positions to the CURRENT CAUSAL SP2 constituents supplied
  by the caller, plus the frozen recovery ETF;
- keep strategy reserve explicitly separate from broker account cash;
- invoke the frozen A4 reserve-first physical target constructor;
- preserve SELL-before-BUY and settlement requirements;
- authorize no broker POST and no live execution.

This module performs:
- no network I/O;
- no database I/O;
- no broker orders.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from sp1execution.recovery.physical_target_v01 import (
    PhysicalTargetError,
    PhysicalTargetPlan,
    construct_physical_target,
)
from sp1execution.recovery.t212_demo_adapter_v01 import (
    ACCOUNT_PRIMARY_CURRENCY,
    RECOVERY_ISIN,
    RECOVERY_TICKER,
    DemoAccountSnapshot,
    DemoPosition,
)


LIVE_EXECUTION_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False

NEW_OPERATIONAL_STATE_RESERVE_EUR = Decimal("0")

_EPS = Decimal("0.00000001")


class DemoPhysicalPlanError(ValueError):
    """Fail-closed A6B1 integration error."""


def _d(
    value: object,
    field: str,
) -> Decimal:

    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise DemoPhysicalPlanError(
            f"{field}: invalid decimal"
        ) from exc

    if not result.is_finite():
        raise DemoPhysicalPlanError(
            f"{field}: non-finite decimal"
        )

    return result


def bootstrap_new_operational_reserve_eur() -> Decimal:
    """
    Reserve for a brand-new operational state.

    No reserve is inferred from:
    - Trading 212 account cash;
    - initial strategy capital;
    - portfolio appreciation;
    - unverified historical dividends.

    Existing reserve must later come from explicit durable strategy state.
    """

    return NEW_OPERATIONAL_STATE_RESERVE_EUR


def _expected_sp2_set(
    tickers: Iterable[str],
) -> tuple[str, str]:

    cleaned = tuple(
        sorted(
            {
                str(ticker).strip()
                for ticker in tickers
                if str(ticker).strip()
            }
        )
    )

    if len(cleaned) != 2:
        raise DemoPhysicalPlanError(
            "current causal SP2 composition must contain exactly 2 tickers"
        )

    if RECOVERY_TICKER in cleaned:
        raise DemoPhysicalPlanError(
            "recovery ticker cannot be an SP2 constituent"
        )

    return cleaned[0], cleaned[1]


def _wallet_current_value_eur(
    position: DemoPosition,
) -> Decimal:

    wallet = position.wallet_impact

    if not isinstance(wallet, Mapping):
        raise DemoPhysicalPlanError(
            f"{position.ticker}: walletImpact missing"
        )

    currency = str(
        wallet.get(
            "currency",
            "",
        )
    ).upper()

    if currency != ACCOUNT_PRIMARY_CURRENCY:
        raise DemoPhysicalPlanError(
            f"{position.ticker}: walletImpact currency "
            f"{currency!r} is not EUR"
        )

    value = _d(
        wallet.get(
            "currentValue"
        ),
        f"{position.ticker}.walletImpact.currentValue",
    )

    if value < 0:
        raise DemoPhysicalPlanError(
            f"{position.ticker}: negative wallet current value"
        )

    return value


def _position_scope(
    positions: Iterable[DemoPosition],
    expected_sp2_tickers: tuple[str, str],
) -> dict[str, DemoPosition]:

    by_ticker: dict[str, DemoPosition] = {}

    for position in positions:

        if position.ticker in by_ticker:
            raise DemoPhysicalPlanError(
                f"duplicate position ticker: {position.ticker}"
            )

        by_ticker[position.ticker] = position

    expected_sp2 = set(
        expected_sp2_tickers
    )

    allowed = (
        expected_sp2
        |
        {RECOVERY_TICKER}
    )

    unrelated = (
        set(by_ticker)
        -
        allowed
    )

    if unrelated:
        raise DemoPhysicalPlanError(
            "positions outside current strategy scope: "
            + repr(
                sorted(unrelated)
            )
        )

    missing = (
        expected_sp2
        -
        set(by_ticker)
    )

    if missing:
        raise DemoPhysicalPlanError(
            "current causal SP2 positions missing: "
            + repr(
                sorted(missing)
            )
        )

    for ticker in expected_sp2_tickers:

        position = by_ticker[ticker]

        if not position.fully_available_for_trading:
            raise DemoPhysicalPlanError(
                f"{ticker}: position is not fully available for trading"
            )

    recovery = by_ticker.get(
        RECOVERY_TICKER
    )

    if (
        recovery is not None
        and
        not recovery.fully_available_for_trading
    ):
        raise DemoPhysicalPlanError(
            f"{RECOVERY_TICKER}: recovery position "
            "is not fully available for trading"
        )

    return by_ticker


@dataclass(frozen=True)
class IntegratedDemoPhysicalPlan:
    schema: str

    expected_sp2_tickers: tuple[str, str]

    sp2_values_eur: tuple[
        tuple[str, Decimal],
        ...
    ]

    current_recovery_eur: Decimal

    strategy_reserve_eur: Decimal

    broker_available_to_trade_eur: Decimal

    broker_cash_outside_strategy_reserve_eur: Decimal

    broker_cash_used_as_strategy_reserve: bool

    physical_target: PhysicalTargetPlan

    funding_feasible_without_sale_proceeds: bool

    requires_sell_fill_before_buy: bool

    requires_sale_proceeds_settlement_confirmation: bool

    buy_authorized_now: bool

    broker_post_authorized: bool

    live_execution_authorized: bool


def build_demo_physical_plan(
    *,
    account: DemoAccountSnapshot,
    positions: Iterable[DemoPosition],
    expected_sp2_tickers: Iterable[str],
    target_recovery_weight: object,
    strategy_reserve_eur: object,
    sp2_return_weights: Mapping[str, object] | None = None,
) -> IntegratedDemoPhysicalPlan:
    """
    Build a physical execution plan without authorizing execution.

    `strategy_reserve_eur` MUST come from explicit strategy-scoped state.

    `account.available_to_trade_eur` is only a broker feasibility ceiling.
    It MUST NOT be treated as strategy reserve.

    The CURRENT CAUSAL SP2 ticker set is supplied by the caller so that
    AAPL/NVDA are not frozen as permanent strategy constituents.
    """

    if account.currency != ACCOUNT_PRIMARY_CURRENCY:
        raise DemoPhysicalPlanError(
            "Trading 212 account is not EUR"
        )

    reserve = _d(
        strategy_reserve_eur,
        "strategy_reserve_eur",
    )

    if reserve < 0:
        raise DemoPhysicalPlanError(
            "negative strategy reserve"
        )

    broker_cash = _d(
        account.available_to_trade_eur,
        "broker_available_to_trade_eur",
    )

    if broker_cash < 0:
        raise DemoPhysicalPlanError(
            "negative broker availableToTrade"
        )

    if reserve > broker_cash + _EPS:
        raise DemoPhysicalPlanError(
            "strategy reserve exceeds broker availableToTrade"
        )

    expected = _expected_sp2_set(
        expected_sp2_tickers
    )

    scoped = _position_scope(
        positions,
        expected,
    )

    sp2_values = {
        ticker:
            _wallet_current_value_eur(
                scoped[ticker]
            )
        for ticker in expected
    }

    recovery_position = scoped.get(
        RECOVERY_TICKER
    )

    recovery_value = (
        Decimal("0")
        if recovery_position is None
        else _wallet_current_value_eur(
            recovery_position
        )
    )

    try:
        target = construct_physical_target(
            target_recovery_weight=
                target_recovery_weight,

            sp2_holdings_eur=
                sp2_values,

            recovery_value_eur=
                recovery_value,

            reserve_available_eur=
                reserve,

            sp2_return_weights=
                sp2_return_weights,
        )

    except PhysicalTargetError as exc:
        raise DemoPhysicalPlanError(
            str(exc)
        ) from exc

    # The broker may contain unrelated account cash, but that cash cannot
    # make a strategy plan funded. Only the explicit strategy reserve may.
    #
    # A4's funding_ready_without_sale_proceeds therefore remains the
    # authoritative strategy-funding condition.
    funding_without_sales = bool(
        target.funding_ready_without_sale_proceeds
    )

    broker_external_cash = (
        broker_cash
        -
        reserve
    )

    if broker_external_cash < 0 and abs(
        broker_external_cash
    ) <= _EPS:
        broker_external_cash = Decimal("0")

    return IntegratedDemoPhysicalPlan(
        schema=
            "sp2_recovery_a6b1_demo_physical_plan_v1",

        expected_sp2_tickers=
            expected,

        sp2_values_eur=
            tuple(
                sorted(
                    sp2_values.items()
                )
            ),

        current_recovery_eur=
            recovery_value,

        strategy_reserve_eur=
            reserve,

        broker_available_to_trade_eur=
            broker_cash,

        broker_cash_outside_strategy_reserve_eur=
            broker_external_cash,

        broker_cash_used_as_strategy_reserve=
            False,

        physical_target=
            target,

        funding_feasible_without_sale_proceeds=
            funding_without_sales,

        requires_sell_fill_before_buy=
            target.requires_sell_fill_before_buy,

        requires_sale_proceeds_settlement_confirmation=
            target.requires_sale_proceeds_settlement_confirmation,

        # A6B1 is planning only. Even a fully funded reserve-only BUY is
        # not yet authorized to POST.
        buy_authorized_now=
            False,

        broker_post_authorized=
            False,

        live_execution_authorized=
            False,
    )
