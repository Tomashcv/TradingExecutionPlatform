"""
A6B2B — causal runtime input boundary.

This module deliberately does NOT fetch market data.

It defines the provenance and causality contract that future runtime
providers must satisfy before a physical Trading 212 plan may be built.

It prevents:
- broker positions from defining SP2 membership;
- frozen historical research files from masquerading as current data;
- future-dated signals;
- arbitrary recovery weights;
- replacement of the frozen CORE_RETURN rule.

No network.
No database.
No broker calls.
No execution authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re


CORE_RETURN_RULE_ID = (
    "SP2_RECOVERY_CORE_RETURN_D40_H378_V1"
)

RECOVERY_TICKER = "SMHm_EQ"

VALIDATED_RUNTIME_PROVIDER = (
    "VALIDATED_RUNTIME_PROVIDER"
)

FROZEN_RESEARCH_REPLAY = (
    "FROZEN_RESEARCH_REPLAY"
)

_ALLOWED_SOURCE_KINDS = {
    VALIDATED_RUNTIME_PROVIDER,
    FROZEN_RESEARCH_REPLAY,
}

_ALLOWED_RECOVERY_WEIGHTS = {
    Decimal("0"),
    Decimal("0.10"),
    Decimal("0.30"),
    Decimal("0.60"),
    Decimal("1.00"),
}

_ALLOWED_RECOVERY_DIRECTIVES = {
    "NORMAL",
    "ACTIVE_TARGET",
    "EXIT_TO_NORMAL",
}

_SHA256_RE = re.compile(
    r"^[0-9a-f]{64}$"
)

LIVE_EXECUTION_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False
BROKER_POSITIONS_CAN_DEFINE_SP2 = False


class CausalRuntimeInputError(ValueError):
    """Fail-closed runtime-input validation error."""


def _date(
    value: object,
    field: str,
) -> date:

    if isinstance(value, datetime):
        raise CausalRuntimeInputError(
            f"{field}: datetime not accepted; "
            "supply explicit causal calendar date"
        )

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(
            str(value)
        )
    except Exception as exc:
        raise CausalRuntimeInputError(
            f"{field}: invalid ISO date"
        ) from exc


def _sha256(
    value: object,
    field: str,
) -> str:

    text = str(value).strip().lower()

    if not _SHA256_RE.fullmatch(text):
        raise CausalRuntimeInputError(
            f"{field}: expected 64-character SHA-256"
        )

    return text


def _source_kind(
    value: object,
) -> str:

    text = str(value).strip().upper()

    if text not in _ALLOWED_SOURCE_KINDS:
        raise CausalRuntimeInputError(
            "source_kind must be a validated runtime provider "
            "or frozen research replay"
        )

    return text


def _source_id(
    value: object,
    field: str,
) -> str:

    text = str(value).strip()

    if not text:
        raise CausalRuntimeInputError(
            f"{field}: empty source id"
        )

    return text


def _weight(
    value: object,
) -> Decimal:

    try:
        result = Decimal(
            str(value)
        )
    except Exception as exc:
        raise CausalRuntimeInputError(
            "target_recovery_weight: invalid decimal"
        ) from exc

    if not result.is_finite():
        raise CausalRuntimeInputError(
            "target_recovery_weight: non-finite"
        )

    if result not in _ALLOWED_RECOVERY_WEIGHTS:
        raise CausalRuntimeInputError(
            "target_recovery_weight is not one of "
            "the frozen ladder states "
            "{0, 0.10, 0.30, 0.60, 1.00}"
        )

    return result


@dataclass(frozen=True)
class RuntimeSP2Composition:
    """
    Current causal SP2 membership decision.

    `ranked_tickers` preserves Top-1 / Top-2 rank order even though
    TRUE HOLD membership-change semantics operate on the two-name set.
    """

    asof_date: date
    signal_date: date
    effective_date: date

    ranked_tickers: tuple[str, str]

    source_kind: str
    source_id: str
    source_sha256: str

    schema: str = (
        "sp2_recovery_a6b2b_runtime_sp2_composition_v1"
    )

    def __post_init__(self) -> None:

        asof = _date(
            self.asof_date,
            "asof_date",
        )

        signal = _date(
            self.signal_date,
            "signal_date",
        )

        effective = _date(
            self.effective_date,
            "effective_date",
        )

        tickers = tuple(
            str(x).strip()
            for x in self.ranked_tickers
        )

        if len(tickers) != 2:
            raise CausalRuntimeInputError(
                "SP2 composition must contain exactly 2 tickers"
            )

        if any(
            not ticker
            for ticker in tickers
        ):
            raise CausalRuntimeInputError(
                "SP2 ticker cannot be empty"
            )

        if len(set(tickers)) != 2:
            raise CausalRuntimeInputError(
                "SP2 tickers must be distinct"
            )

        if RECOVERY_TICKER in tickers:
            raise CausalRuntimeInputError(
                "recovery ETF cannot define SP2 membership"
            )

        if signal > effective:
            raise CausalRuntimeInputError(
                "SP2 signal_date cannot follow effective_date"
            )

        if effective > asof:
            raise CausalRuntimeInputError(
                "SP2 effective_date is in the future"
            )

        object.__setattr__(
            self,
            "asof_date",
            asof,
        )

        object.__setattr__(
            self,
            "signal_date",
            signal,
        )

        object.__setattr__(
            self,
            "effective_date",
            effective,
        )

        object.__setattr__(
            self,
            "ranked_tickers",
            tickers,
        )

        object.__setattr__(
            self,
            "source_kind",
            _source_kind(
                self.source_kind
            ),
        )

        object.__setattr__(
            self,
            "source_id",
            _source_id(
                self.source_id,
                "source_id",
            ),
        )

        object.__setattr__(
            self,
            "source_sha256",
            _sha256(
                self.source_sha256,
                "source_sha256",
            ),
        )

    @property
    def runtime_eligible(self) -> bool:
        return (
            self.source_kind
            ==
            VALIDATED_RUNTIME_PROVIDER
        )


@dataclass(frozen=True)
class RuntimeRecoveryTarget:
    """
    Current target state emitted by the frozen CORE_RETURN runtime
    state machine/provider.
    """

    asof_date: date
    effective_date: date

    target_recovery_weight: Decimal

    directive: str

    source_kind: str
    source_id: str
    source_sha256: str

    rule_id: str = CORE_RETURN_RULE_ID

    schema: str = (
        "sp2_recovery_a6b2b_runtime_recovery_target_v1"
    )

    def __post_init__(self) -> None:

        asof = _date(
            self.asof_date,
            "asof_date",
        )

        effective = _date(
            self.effective_date,
            "effective_date",
        )

        if effective > asof:
            raise CausalRuntimeInputError(
                "recovery effective_date is in the future"
            )

        rule_id = str(
            self.rule_id
        ).strip()

        if rule_id != CORE_RETURN_RULE_ID:
            raise CausalRuntimeInputError(
                "unexpected recovery rule id"
            )

        directive = str(
            self.directive
        ).strip().upper()

        if directive not in _ALLOWED_RECOVERY_DIRECTIVES:
            raise CausalRuntimeInputError(
                "invalid recovery directive"
            )

        weight = _weight(
            self.target_recovery_weight
        )

        if (
            directive
            in {
                "NORMAL",
                "EXIT_TO_NORMAL",
            }
            and
            weight != Decimal("0")
        ):
            raise CausalRuntimeInputError(
                f"{directive} requires zero recovery weight"
            )

        if (
            directive
            ==
            "ACTIVE_TARGET"
            and
            weight == Decimal("0")
        ):
            raise CausalRuntimeInputError(
                "ACTIVE_TARGET requires positive recovery weight"
            )

        object.__setattr__(
            self,
            "asof_date",
            asof,
        )

        object.__setattr__(
            self,
            "effective_date",
            effective,
        )

        object.__setattr__(
            self,
            "target_recovery_weight",
            weight,
        )

        object.__setattr__(
            self,
            "directive",
            directive,
        )

        object.__setattr__(
            self,
            "source_kind",
            _source_kind(
                self.source_kind
            ),
        )

        object.__setattr__(
            self,
            "source_id",
            _source_id(
                self.source_id,
                "source_id",
            ),
        )

        object.__setattr__(
            self,
            "source_sha256",
            _sha256(
                self.source_sha256,
                "source_sha256",
            ),
        )

        object.__setattr__(
            self,
            "rule_id",
            rule_id,
        )

    @property
    def runtime_eligible(self) -> bool:
        return (
            self.source_kind
            ==
            VALIDATED_RUNTIME_PROVIDER
        )


@dataclass(frozen=True)
class CausalRuntimeInputs:
    """
    Fully validated CURRENT runtime strategy inputs.

    Frozen replay material is explicitly rejected here.
    """

    asof_date: date

    sp2: RuntimeSP2Composition
    recovery: RuntimeRecoveryTarget

    broker_positions_define_sp2: bool = False

    broker_post_authorized: bool = False
    live_execution_authorized: bool = False

    schema: str = (
        "sp2_recovery_a6b2b_causal_runtime_inputs_v1"
    )

    def __post_init__(self) -> None:

        asof = _date(
            self.asof_date,
            "asof_date",
        )

        if self.sp2.asof_date != asof:
            raise CausalRuntimeInputError(
                "SP2 input asof_date mismatch"
            )

        if self.recovery.asof_date != asof:
            raise CausalRuntimeInputError(
                "recovery input asof_date mismatch"
            )

        if not self.sp2.runtime_eligible:
            raise CausalRuntimeInputError(
                "frozen historical SP2 replay is not "
                "eligible as current runtime membership"
            )

        if not self.recovery.runtime_eligible:
            raise CausalRuntimeInputError(
                "frozen historical recovery replay is not "
                "eligible as current runtime target"
            )

        if self.broker_positions_define_sp2:
            raise CausalRuntimeInputError(
                "broker positions cannot define SP2 membership"
            )

        if self.broker_post_authorized:
            raise CausalRuntimeInputError(
                "A6B2B cannot authorize broker POST"
            )

        if self.live_execution_authorized:
            raise CausalRuntimeInputError(
                "A6B2B cannot authorize live execution"
            )

        object.__setattr__(
            self,
            "asof_date",
            asof,
        )

    @property
    def sp2_tickers(self) -> tuple[str, str]:
        return self.sp2.ranked_tickers

    @property
    def recovery_target_weight(self) -> Decimal:
        return self.recovery.target_recovery_weight
