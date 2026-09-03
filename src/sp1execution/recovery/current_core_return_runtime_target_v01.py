"""
A6C2D — current CORE_RETURN runtime target adapter.

This module converts validated stateful-replay evidence into the
already-frozen A6B2B ``RuntimeRecoveryTarget`` ABI.

It does NOT:

- fetch market data;
- replay historical data itself;
- create a database;
- inspect broker positions;
- derive strategy reserve;
- perform broker GET/POST;
- create orders;
- authorize live execution.

A state snapshot is not a transition event.

Therefore this adapter emits:

- RECOVERY_ACTIVE -> ACTIVE_TARGET
- NORMAL -> NORMAL
- WAIT_D40 -> NORMAL
- OLD_ATH_GUARD -> NORMAL

It deliberately does NOT infer EXIT_TO_NORMAL from a state snapshot.
That directive requires explicit transition evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256

import json
import math
import re


from sp1execution.recovery.causal_runtime_inputs_v01 import (
    CORE_RETURN_RULE_ID,
    VALIDATED_RUNTIME_PROVIDER,
    RuntimeRecoveryTarget,
)


SOURCE_PROVIDER_ID = (
    "CORE_RETURN_STATEFUL_REPLAY_RUNTIME_TARGET_V1"
)

MARKET_PROVIDER_ID = (
    "YAHOO_CHART_IVV_ADJCLOSE_FULLHISTORY_BRIDGE_V1"
)

_ALLOWED_PHASES = frozenset({
    "NORMAL",
    "WAIT_D40",
    "RECOVERY_ACTIVE",
    "OLD_ATH_GUARD",
})

_ALLOWED_TARGETS = frozenset({
    Decimal("0"),
    Decimal("0.10"),
    Decimal("0.30"),
    Decimal("0.60"),
    Decimal("1.00"),
})

_HEX64 = re.compile(
    r"^[0-9a-f]{64}$"
)

NETWORK_PERFORMED_BY_PROVIDER = False
DATABASE_CREATED_BY_PROVIDER = False
BROKER_GET_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False
LIVE_EXECUTION_AUTHORIZED = False

REPLAY_RESERVE_BUCKET_CAN_DEFINE_STRATEGY_RESERVE = False
EXIT_TO_NORMAL_INFERRED_FROM_STATE_SNAPSHOT = False


class CurrentCoreReturnTargetError(
    ValueError
):
    """Fail-closed runtime-target adapter error."""


def _date(
    value: object,
    field: str,
) -> date:

    if isinstance(
        value,
        datetime,
    ):
        raise CurrentCoreReturnTargetError(
            f"{field}: datetime is not accepted"
        )

    if isinstance(
        value,
        date,
    ):
        return value

    try:
        return date.fromisoformat(
            str(value)
        )

    except Exception as exc:
        raise CurrentCoreReturnTargetError(
            f"{field}: invalid ISO date"
        ) from exc


def _decimal(
    value: object,
    field: str,
) -> Decimal:

    try:
        result = Decimal(
            str(value)
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ) as exc:
        raise CurrentCoreReturnTargetError(
            f"{field}: invalid Decimal"
        ) from exc

    if not result.is_finite():
        raise CurrentCoreReturnTargetError(
            f"{field}: non-finite Decimal"
        )

    return result


def _optional_decimal(
    value: object,
    field: str,
) -> Decimal | None:

    if value is None:
        return None

    return _decimal(
        value,
        field,
    )


def _optional_session(
    value: object,
    field: str,
) -> str | None:

    if value is None:
        return None

    text = str(
        value
    ).strip()

    if not text:
        raise CurrentCoreReturnTargetError(
            f"{field}: empty session"
        )

    _date(
        text,
        field,
    )

    return text


def _sha256(
    value: object,
    field: str,
) -> str:

    text = str(
        value
    ).strip().lower()

    if not _HEX64.fullmatch(
        text
    ):
        raise CurrentCoreReturnTargetError(
            f"{field}: expected lowercase SHA-256"
        )

    return text


def _nonnegative_int(
    value: object,
    field: str,
) -> int:

    if isinstance(
        value,
        bool,
    ):
        raise CurrentCoreReturnTargetError(
            f"{field}: bool is not accepted"
        )

    try:
        result = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:
        raise CurrentCoreReturnTargetError(
            f"{field}: invalid integer"
        ) from exc

    if result < 0:
        raise CurrentCoreReturnTargetError(
            f"{field}: negative integer"
        )

    return result


@dataclass(frozen=True)
class CurrentCoreReturnReplayEvidence:
    """
    Semantic evidence produced by the validated current stateful replay.

    Volatile audit timestamps are intentionally absent.
    """

    runtime_asof_date: date
    effective_date: date

    phase: str
    current_target: Decimal

    cycle_id: str | None

    first_actual_entry_session: str | None
    fixed_exit_session: str | None

    old_ath: Decimal | None
    old_ath_recovered: bool

    open_d40_event_count: int
    state_revision: int

    current_source_cycle_count: int
    post_anchor_source_event_count: int

    current_final_event_count: int
    post_anchor_final_event_count: int

    semantic_state_deterministic: bool
    pending_inventory_deterministic: bool
    transition_inventory_deterministic: bool
    state_machine_consistent: bool

    market_provider_id: str
    market_provider_decision_sha256: str
    stitched_series_sha256: str
    replay_decision_sha256: str

    def __post_init__(self) -> None:

        asof = _date(
            self.runtime_asof_date,
            "runtime_asof_date",
        )

        effective = _date(
            self.effective_date,
            "effective_date",
        )

        if effective > asof:
            raise CurrentCoreReturnTargetError(
                "effective_date is after runtime_asof_date"
            )

        phase = str(
            self.phase
        ).strip().upper()

        if phase not in _ALLOWED_PHASES:
            raise CurrentCoreReturnTargetError(
                "invalid CORE_RETURN phase"
            )

        target = _decimal(
            self.current_target,
            "current_target",
        )

        if target not in _ALLOWED_TARGETS:
            raise CurrentCoreReturnTargetError(
                "current_target is outside frozen recovery ladder"
            )

        cycle_id = self.cycle_id

        if cycle_id is not None:

            cycle_id = str(
                cycle_id
            ).strip()

            if not cycle_id:
                raise CurrentCoreReturnTargetError(
                    "cycle_id is empty"
                )

        first_entry = _optional_session(
            self.first_actual_entry_session,
            "first_actual_entry_session",
        )

        fixed_exit = _optional_session(
            self.fixed_exit_session,
            "fixed_exit_session",
        )

        old_ath = _optional_decimal(
            self.old_ath,
            "old_ath",
        )

        if (
            old_ath is not None
            and
            old_ath <= 0
        ):
            raise CurrentCoreReturnTargetError(
                "old_ath must be positive"
            )

        if not isinstance(
            self.old_ath_recovered,
            bool,
        ):
            raise CurrentCoreReturnTargetError(
                "old_ath_recovered must be bool"
            )

        open_d40 = _nonnegative_int(
            self.open_d40_event_count,
            "open_d40_event_count",
        )

        revision = _nonnegative_int(
            self.state_revision,
            "state_revision",
        )

        source_cycles = _nonnegative_int(
            self.current_source_cycle_count,
            "current_source_cycle_count",
        )

        post_source = _nonnegative_int(
            self.post_anchor_source_event_count,
            "post_anchor_source_event_count",
        )

        final_events = _nonnegative_int(
            self.current_final_event_count,
            "current_final_event_count",
        )

        post_final = _nonnegative_int(
            self.post_anchor_final_event_count,
            "post_anchor_final_event_count",
        )

        for field in (
            "semantic_state_deterministic",
            "pending_inventory_deterministic",
            "transition_inventory_deterministic",
            "state_machine_consistent",
        ):

            value = getattr(
                self,
                field,
            )

            if value is not True:
                raise CurrentCoreReturnTargetError(
                    f"{field} must be true"
                )

        market_provider = str(
            self.market_provider_id
        ).strip()

        if market_provider != MARKET_PROVIDER_ID:
            raise CurrentCoreReturnTargetError(
                "unexpected current IVV market provider"
            )

        market_sha = _sha256(
            self.market_provider_decision_sha256,
            "market_provider_decision_sha256",
        )

        stitched_sha = _sha256(
            self.stitched_series_sha256,
            "stitched_series_sha256",
        )

        replay_sha = _sha256(
            self.replay_decision_sha256,
            "replay_decision_sha256",
        )

        # ------------------------------------------------------------------
        # Frozen state-machine consistency.
        # ------------------------------------------------------------------

        if phase == "NORMAL":

            if target != Decimal("0"):
                raise CurrentCoreReturnTargetError(
                    "NORMAL requires zero current target"
                )

            if open_d40 != 0:
                raise CurrentCoreReturnTargetError(
                    "NORMAL cannot have open D40 events"
                )

            if cycle_id is not None:
                raise CurrentCoreReturnTargetError(
                    "NORMAL cannot retain a cycle_id"
                )

            if first_entry is not None:
                raise CurrentCoreReturnTargetError(
                    "NORMAL cannot retain first entry"
                )

            if fixed_exit is not None:
                raise CurrentCoreReturnTargetError(
                    "NORMAL cannot retain fixed exit"
                )

            if old_ath is not None:
                raise CurrentCoreReturnTargetError(
                    "NORMAL cannot retain old ATH"
                )

            if self.old_ath_recovered:
                raise CurrentCoreReturnTargetError(
                    "NORMAL cannot retain old_ath_recovered flag"
                )

        elif phase == "WAIT_D40":

            if target != Decimal("0"):
                raise CurrentCoreReturnTargetError(
                    "WAIT_D40 requires zero current target"
                )

            if open_d40 < 1:
                raise CurrentCoreReturnTargetError(
                    "WAIT_D40 requires an open D40 event"
                )

            if cycle_id is None:
                raise CurrentCoreReturnTargetError(
                    "WAIT_D40 requires cycle_id"
                )

            if old_ath is None:
                raise CurrentCoreReturnTargetError(
                    "WAIT_D40 requires old ATH"
                )

            if first_entry is not None:
                raise CurrentCoreReturnTargetError(
                    "WAIT_D40 cannot have first actual entry"
                )

            if fixed_exit is not None:
                raise CurrentCoreReturnTargetError(
                    "WAIT_D40 cannot have fixed exit"
                )

        elif phase == "RECOVERY_ACTIVE":

            if target == Decimal("0"):
                raise CurrentCoreReturnTargetError(
                    "RECOVERY_ACTIVE requires positive target"
                )

            if cycle_id is None:
                raise CurrentCoreReturnTargetError(
                    "RECOVERY_ACTIVE requires cycle_id"
                )

            if old_ath is None:
                raise CurrentCoreReturnTargetError(
                    "RECOVERY_ACTIVE requires old ATH"
                )

            if first_entry is None:
                raise CurrentCoreReturnTargetError(
                    "RECOVERY_ACTIVE requires first entry"
                )

            if fixed_exit is None:
                raise CurrentCoreReturnTargetError(
                    "RECOVERY_ACTIVE requires fixed exit"
                )

        elif phase == "OLD_ATH_GUARD":

            if target != Decimal("0"):
                raise CurrentCoreReturnTargetError(
                    "OLD_ATH_GUARD requires zero target"
                )

            if open_d40 != 0:
                raise CurrentCoreReturnTargetError(
                    "OLD_ATH_GUARD cannot have open D40 events"
                )

            if cycle_id is None:
                raise CurrentCoreReturnTargetError(
                    "OLD_ATH_GUARD requires cycle_id"
                )

            if old_ath is None:
                raise CurrentCoreReturnTargetError(
                    "OLD_ATH_GUARD requires old ATH"
                )

        object.__setattr__(
            self,
            "runtime_asof_date",
            asof,
        )

        object.__setattr__(
            self,
            "effective_date",
            effective,
        )

        object.__setattr__(
            self,
            "phase",
            phase,
        )

        object.__setattr__(
            self,
            "current_target",
            target,
        )

        object.__setattr__(
            self,
            "cycle_id",
            cycle_id,
        )

        object.__setattr__(
            self,
            "first_actual_entry_session",
            first_entry,
        )

        object.__setattr__(
            self,
            "fixed_exit_session",
            fixed_exit,
        )

        object.__setattr__(
            self,
            "old_ath",
            old_ath,
        )

        object.__setattr__(
            self,
            "open_d40_event_count",
            open_d40,
        )

        object.__setattr__(
            self,
            "state_revision",
            revision,
        )

        object.__setattr__(
            self,
            "current_source_cycle_count",
            source_cycles,
        )

        object.__setattr__(
            self,
            "post_anchor_source_event_count",
            post_source,
        )

        object.__setattr__(
            self,
            "current_final_event_count",
            final_events,
        )

        object.__setattr__(
            self,
            "post_anchor_final_event_count",
            post_final,
        )

        object.__setattr__(
            self,
            "market_provider_id",
            market_provider,
        )

        object.__setattr__(
            self,
            "market_provider_decision_sha256",
            market_sha,
        )

        object.__setattr__(
            self,
            "stitched_series_sha256",
            stitched_sha,
        )

        object.__setattr__(
            self,
            "replay_decision_sha256",
            replay_sha,
        )


def _directive_for_snapshot(
    evidence: CurrentCoreReturnReplayEvidence,
) -> str:

    if evidence.phase == "RECOVERY_ACTIVE":
        return "ACTIVE_TARGET"

    # A state snapshot does not prove that an exit transition happened
    # on this exact session. Do not fabricate EXIT_TO_NORMAL.
    return "NORMAL"


def _evidence_payload(
    evidence: CurrentCoreReturnReplayEvidence,
) -> dict[str, object]:

    return {
        "schema":
            "sp2_recovery_a6c2d_core_return_replay_evidence_v1",

        "runtime_asof_date":
            evidence.runtime_asof_date.isoformat(),

        "effective_date":
            evidence.effective_date.isoformat(),

        "phase":
            evidence.phase,

        "current_target":
            format(
                evidence.current_target,
                "f",
            ),

        "cycle_id":
            evidence.cycle_id,

        "first_actual_entry_session":
            evidence.first_actual_entry_session,

        "fixed_exit_session":
            evidence.fixed_exit_session,

        "old_ath":
            (
                None
                if evidence.old_ath is None
                else format(
                    evidence.old_ath,
                    "f",
                )
            ),

        "old_ath_recovered":
            evidence.old_ath_recovered,

        "open_d40_event_count":
            evidence.open_d40_event_count,

        "state_revision":
            evidence.state_revision,

        "current_source_cycle_count":
            evidence.current_source_cycle_count,

        "post_anchor_source_event_count":
            evidence.post_anchor_source_event_count,

        "current_final_event_count":
            evidence.current_final_event_count,

        "post_anchor_final_event_count":
            evidence.post_anchor_final_event_count,

        "semantic_state_deterministic":
            evidence.semantic_state_deterministic,

        "pending_inventory_deterministic":
            evidence.pending_inventory_deterministic,

        "transition_inventory_deterministic":
            evidence.transition_inventory_deterministic,

        "state_machine_consistent":
            evidence.state_machine_consistent,

        "market_provider_id":
            evidence.market_provider_id,

        "market_provider_decision_sha256":
            evidence.market_provider_decision_sha256,

        "stitched_series_sha256":
            evidence.stitched_series_sha256,

        "replay_decision_sha256":
            evidence.replay_decision_sha256,

        "rule_id":
            CORE_RETURN_RULE_ID,
    }


def evidence_sha256(
    evidence: CurrentCoreReturnReplayEvidence,
) -> str:

    payload = _evidence_payload(
        evidence
    )

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
    ).encode(
        "utf-8"
    )

    return sha256(
        encoded
    ).hexdigest()


def build_runtime_recovery_target_from_replay_evidence(
    evidence: CurrentCoreReturnReplayEvidence,
) -> RuntimeRecoveryTarget:

    if not isinstance(
        evidence,
        CurrentCoreReturnReplayEvidence,
    ):
        raise CurrentCoreReturnTargetError(
            "evidence must be CurrentCoreReturnReplayEvidence"
        )

    directive = _directive_for_snapshot(
        evidence
    )

    source_sha = evidence_sha256(
        evidence
    )

    source_id = (
        f"{SOURCE_PROVIDER_ID}:"
        f"{evidence.effective_date.isoformat()}:"
        f"{evidence.phase}"
    )

    target = RuntimeRecoveryTarget(
        asof_date=
            evidence.runtime_asof_date,

        effective_date=
            evidence.effective_date,

        target_recovery_weight=
            evidence.current_target,

        directive=
            directive,

        source_kind=
            VALIDATED_RUNTIME_PROVIDER,

        source_id=
            source_id,

        source_sha256=
            source_sha,

        rule_id=
            CORE_RETURN_RULE_ID,
    )

    if not target.runtime_eligible:
        raise CurrentCoreReturnTargetError(
            "constructed recovery target is not runtime eligible"
        )

    return target
