"""
A4 reserve-first physical target constructor.

Pure strategy-to-physical-plan adapter.

This module:
- performs no broker calls;
- performs no database writes;
- performs no network I/O;
- does not authorize live execution;
- does not assume unsettled sale proceeds are reusable;
- does not assume a 50/50 SP2 return composition.

The recovery instrument is identified at the ISIN layer only.
Broker-specific listing/symbol mapping is deliberately deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


RECOVERY_INSTRUMENT_ISIN = "IE00BMC38736"
RESEARCH_PROXY = "SOXX"

LIVE_EXECUTION_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False

_EPS = Decimal("0.000000001")
_ONE = Decimal("1")


class PhysicalTargetError(ValueError):
    """Fail-closed A4 planning error."""


def _d(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise PhysicalTargetError(
            f"invalid decimal value: {value!r}"
        ) from exc

    if not result.is_finite():
        raise PhysicalTargetError(
            f"non-finite decimal value: {value!r}"
        )

    return result


def _clean_nonnegative_mapping(
    values: Mapping[str, object] | None,
    *,
    field_name: str,
) -> dict[str, Decimal]:

    if values is None:
        return {}

    result: dict[str, Decimal] = {}

    for raw_symbol, raw_value in values.items():
        symbol = str(raw_symbol).strip()

        if not symbol:
            raise PhysicalTargetError(
                f"{field_name}: empty symbol"
            )

        if symbol in result:
            raise PhysicalTargetError(
                f"{field_name}: duplicate symbol {symbol}"
            )

        value = _d(raw_value)

        if value < 0:
            raise PhysicalTargetError(
                f"{field_name}: negative value for {symbol}"
            )

        result[symbol] = value

    return dict(
        sorted(
            result.items()
        )
    )


def _sum(
    values: Mapping[str, Decimal],
) -> Decimal:
    return sum(
        values.values(),
        Decimal("0"),
    )


def _validated_weights(
    weights: Mapping[str, object] | None,
) -> dict[str, Decimal]:

    cleaned = _clean_nonnegative_mapping(
        weights,
        field_name="sp2_return_weights",
    )

    if not cleaned:
        raise PhysicalTargetError(
            "current causal SP2 return weights are required "
            "for any recovery->SP2 rotation"
        )

    total = _sum(cleaned)

    if total <= 0:
        raise PhysicalTargetError(
            "SP2 return weights have non-positive total"
        )

    if abs(total - _ONE) > Decimal("0.000001"):
        raise PhysicalTargetError(
            f"SP2 return weights must sum to 1; got {total}"
        )

    # Normalize tiny floating/string representation drift after validation.
    return {
        symbol: value / total
        for symbol, value in cleaned.items()
    }


def _allocate_exact(
    amount: Decimal,
    weights: Mapping[str, Decimal],
) -> tuple[tuple[str, Decimal], ...]:

    if amount < 0:
        raise PhysicalTargetError(
            "cannot allocate negative amount"
        )

    if amount <= _EPS:
        return ()

    keys = sorted(weights)

    if not keys:
        raise PhysicalTargetError(
            "allocation requires at least one symbol"
        )

    rows: list[tuple[str, Decimal]] = []

    remaining = amount

    for symbol in keys[:-1]:
        allocation = (
            amount
            *
            weights[symbol]
        )

        rows.append(
            (
                symbol,
                allocation,
            )
        )

        remaining -= allocation

    rows.append(
        (
            keys[-1],
            remaining,
        )
    )

    return tuple(rows)


def _current_sp2_weights(
    holdings: Mapping[str, Decimal],
) -> dict[str, Decimal]:

    total = _sum(holdings)

    if total <= _EPS:
        raise PhysicalTargetError(
            "cannot fund recovery from SP2: "
            "no positive SP2 market value"
        )

    return {
        symbol: value / total
        for symbol, value in holdings.items()
        if value > 0
    }


@dataclass(frozen=True)
class PhysicalTargetPlan:
    schema: str

    recovery_instrument_isin: str
    research_proxy: str

    direction: str

    target_recovery_weight: Decimal
    total_strategy_nav_eur: Decimal

    current_sp2_eur: Decimal
    current_recovery_eur: Decimal
    reserve_before_eur: Decimal

    target_recovery_eur: Decimal

    reserve_used_eur: Decimal
    reserve_after_eur: Decimal

    sp2_sale_total_eur: Decimal
    sp2_sales_eur: tuple[tuple[str, Decimal], ...]

    recovery_buy_eur: Decimal
    recovery_sell_eur: Decimal

    sp2_buy_total_eur: Decimal
    sp2_buys_eur: tuple[tuple[str, Decimal], ...]

    requires_sell_fill_before_buy: bool
    requires_sale_proceeds_settlement_confirmation: bool

    funding_ready_without_sale_proceeds: bool

    nominal_pre_execution_notional: bool

    broker_post_authorized: bool
    live_execution_authorized: bool


def construct_physical_target(
    *,
    target_recovery_weight: object,
    sp2_holdings_eur: Mapping[str, object] | None,
    recovery_value_eur: object,
    reserve_available_eur: object,
    sp2_return_weights: Mapping[str, object] | None = None,
) -> PhysicalTargetPlan:
    """
    Construct a fail-closed physical target plan.

    `reserve_available_eur` means reserve capital already known by the
    caller to be usable/settled for funding purposes.

    The planner does NOT infer broker settlement rules.

    For increasing the recovery sleeve:
        reserve first,
        then proportional SP2 sales.

    For decreasing the recovery sleeve:
        sell recovery sleeve,
        then return nominal proceeds to externally supplied CURRENT
        CAUSAL SP2 composition.

    No 50/50 fallback exists.
    """

    target_weight = _d(
        target_recovery_weight
    )

    if (
        target_weight < 0
        or
        target_weight > 1
    ):
        raise PhysicalTargetError(
            "target_recovery_weight must be in [0, 1]"
        )

    holdings = _clean_nonnegative_mapping(
        sp2_holdings_eur,
        field_name="sp2_holdings_eur",
    )

    sp2_total = _sum(
        holdings
    )

    recovery_value = _d(
        recovery_value_eur
    )

    reserve = _d(
        reserve_available_eur
    )

    if recovery_value < 0:
        raise PhysicalTargetError(
            "negative recovery value"
        )

    if reserve < 0:
        raise PhysicalTargetError(
            "negative reserve value"
        )

    total_nav = (
        sp2_total
        +
        recovery_value
        +
        reserve
    )

    if total_nav <= 0:
        raise PhysicalTargetError(
            "total strategy NAV must be positive"
        )

    target_recovery = (
        total_nav
        *
        target_weight
    )

    delta = (
        target_recovery
        -
        recovery_value
    )


    # ------------------------------------------------------------------
    # NO CHANGE
    # ------------------------------------------------------------------

    if abs(delta) <= _EPS:

        return PhysicalTargetPlan(
            schema=
                "sp2_recovery_a4_physical_target_v1",

            recovery_instrument_isin=
                RECOVERY_INSTRUMENT_ISIN,

            research_proxy=
                RESEARCH_PROXY,

            direction=
                "NO_CHANGE",

            target_recovery_weight=
                target_weight,

            total_strategy_nav_eur=
                total_nav,

            current_sp2_eur=
                sp2_total,

            current_recovery_eur=
                recovery_value,

            reserve_before_eur=
                reserve,

            target_recovery_eur=
                target_recovery,

            reserve_used_eur=
                Decimal("0"),

            reserve_after_eur=
                reserve,

            sp2_sale_total_eur=
                Decimal("0"),

            sp2_sales_eur=
                (),

            recovery_buy_eur=
                Decimal("0"),

            recovery_sell_eur=
                Decimal("0"),

            sp2_buy_total_eur=
                Decimal("0"),

            sp2_buys_eur=
                (),

            requires_sell_fill_before_buy=
                False,

            requires_sale_proceeds_settlement_confirmation=
                False,

            funding_ready_without_sale_proceeds=
                True,

            nominal_pre_execution_notional=
                True,

            broker_post_authorized=
                False,

            live_execution_authorized=
                False,
        )


    # ------------------------------------------------------------------
    # INCREASE / SCALE-UP RECOVERY
    # ------------------------------------------------------------------

    if delta > 0:

        reserve_used = min(
            reserve,
            delta,
        )

        sale_needed = (
            delta
            -
            reserve_used
        )

        if (
            sale_needed
            >
            sp2_total + _EPS
        ):
            raise PhysicalTargetError(
                "insufficient SP2 value after reserve-first funding"
            )

        if sale_needed <= _EPS:

            sales = ()

        else:

            weights = _current_sp2_weights(
                holdings
            )

            sales = _allocate_exact(
                sale_needed,
                weights,
            )

        reserve_after = (
            reserve
            -
            reserve_used
        )

        requires_sales = (
            sale_needed
            >
            _EPS
        )

        return PhysicalTargetPlan(
            schema=
                "sp2_recovery_a4_physical_target_v1",

            recovery_instrument_isin=
                RECOVERY_INSTRUMENT_ISIN,

            research_proxy=
                RESEARCH_PROXY,

            direction=
                "INCREASE_RECOVERY",

            target_recovery_weight=
                target_weight,

            total_strategy_nav_eur=
                total_nav,

            current_sp2_eur=
                sp2_total,

            current_recovery_eur=
                recovery_value,

            reserve_before_eur=
                reserve,

            target_recovery_eur=
                target_recovery,

            reserve_used_eur=
                reserve_used,

            reserve_after_eur=
                reserve_after,

            sp2_sale_total_eur=
                sale_needed,

            sp2_sales_eur=
                sales,

            recovery_buy_eur=
                delta,

            recovery_sell_eur=
                Decimal("0"),

            sp2_buy_total_eur=
                Decimal("0"),

            sp2_buys_eur=
                (),

            requires_sell_fill_before_buy=
                requires_sales,

            requires_sale_proceeds_settlement_confirmation=
                requires_sales,

            funding_ready_without_sale_proceeds=
                not requires_sales,

            nominal_pre_execution_notional=
                True,

            broker_post_authorized=
                False,

            live_execution_authorized=
                False,
        )


    # ------------------------------------------------------------------
    # DECREASE / H378 EXIT
    # ------------------------------------------------------------------

    recovery_sell = (
        -delta
    )

    return_weights = _validated_weights(
        sp2_return_weights
    )

    sp2_buys = _allocate_exact(
        recovery_sell,
        return_weights,
    )

    return PhysicalTargetPlan(
        schema=
            "sp2_recovery_a4_physical_target_v1",

        recovery_instrument_isin=
            RECOVERY_INSTRUMENT_ISIN,

        research_proxy=
            RESEARCH_PROXY,

        direction=
            "DECREASE_RECOVERY",

        target_recovery_weight=
            target_weight,

        total_strategy_nav_eur=
            total_nav,

        current_sp2_eur=
            sp2_total,

        current_recovery_eur=
            recovery_value,

        reserve_before_eur=
            reserve,

        target_recovery_eur=
            target_recovery,

        reserve_used_eur=
            Decimal("0"),

        reserve_after_eur=
            reserve,

        sp2_sale_total_eur=
            Decimal("0"),

        sp2_sales_eur=
            (),

        recovery_buy_eur=
            Decimal("0"),

        recovery_sell_eur=
            recovery_sell,

        sp2_buy_total_eur=
            recovery_sell,

        sp2_buys_eur=
            sp2_buys,

        requires_sell_fill_before_buy=
            True,

        requires_sale_proceeds_settlement_confirmation=
            True,

        funding_ready_without_sale_proceeds=
            False,

        nominal_pre_execution_notional=
            True,

        broker_post_authorized=
            False,

        live_execution_authorized=
            False,
    )
