from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from sp1execution.recovery.causal_compiler_v01 import (
    CausalCompilerError,
    RecoveryInputRow,
    SourceEvent,
    canonical_sessions,
    compile_source_cycles,
    next_trading_session,
    session_position_map,
    source_events,
)
from sp1execution.recovery.core_return_v01 import (
    delayed_entry_session,
    fixed_exit_session,
    validate_frozen_target,
)
from sp1execution.recovery.state_v01 import (
    RecoveryStateError,
    enqueue_delayed_event,
    initialize_recovery_state,
    load_recovery_state,
    pending_events,
    set_delayed_event_status,
    transition_recovery_state,
    validate_recovery_state,
)


TOL = 1e-9


class DurableDispatcherError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceDispatchResult:
    source_event_key: str
    status: str
    maturity_session: str | None


@dataclass(frozen=True)
class SessionDispatchResult:
    session: str
    applied_events: tuple[str, ...]
    fixed_exit_applied: bool
    rearmed: bool


@dataclass(frozen=True)
class CloseDispatchResult:
    session: str
    status: str


def source_event_key(
    event: SourceEvent,
) -> str:
    return (
        f"cycle:{event.cycle_id}:"
        f"source:{event.signal_date}:"
        f"{event.execution_date}:"
        f"{event.target_sleeve:.2f}"
    )


def _position(
    sessions: Sequence[str],
    session: str,
) -> int:
    positions = session_position_map(
        sessions
    )

    if session not in positions:
        raise DurableDispatcherError(
            f"session absent from canonical calendar: {session}"
        )

    return int(
        positions[
            session
        ]
    )


def _clear_cycle_updates() -> dict[str, Any]:
    return {
        "cycle_id": None,
        "old_ath": None,
        "current_target": 0.0,
        "first_actual_entry_session": None,
        "fixed_exit_session": None,
        "old_ath_recovered": 0,
    }


def _cancel_open_events(
    con: sqlite3.Connection,
    *,
    cycle_id: str,
    at_or_after_session: str | None = None,
    sessions: Sequence[str] | None = None,
) -> tuple[str, ...]:
    rows = pending_events(
        con,
        cycle_id=cycle_id,
    )

    cancelled: list[str] = []

    threshold_position: int | None = None

    if at_or_after_session is not None:
        if sessions is None:
            raise DurableDispatcherError(
                "sessions required for bounded cancellation"
            )

        threshold_position = _position(
            sessions,
            at_or_after_session,
        )

    for row in rows:
        should_cancel = True

        if threshold_position is not None:
            maturity_position = _position(
                sessions,
                str(
                    row[
                        "maturity_session"
                    ]
                ),
            )

            should_cancel = (
                maturity_position
                >=
                threshold_position
            )

        if not should_cancel:
            continue

        set_delayed_event_status(
            con,
            source_event_key=str(
                row[
                    "source_event_key"
                ]
            ),
            to_status="CANCELLED",
        )

        cancelled.append(
            str(
                row[
                    "source_event_key"
                ]
            )
        )

    return tuple(
        cancelled
    )


def ingest_source_event(
    con: sqlite3.Connection,
    *,
    event: SourceEvent,
    observed_session: str,
    sessions: Sequence[str],
) -> SourceDispatchResult:
    """
    Persist one causal source event observed at close T.

    HANDOFF events are source-cycle segmentation only and create no
    CORE_RETURN delayed event.
    """
    if observed_session != event.signal_date:
        raise DurableDispatcherError(
            "source event must be ingested on its causal signal session"
        )

    expected_t_plus_1 = next_trading_session(
        sessions,
        event.signal_date,
    )

    if event.execution_date != expected_t_plus_1:
        raise DurableDispatcherError(
            "source execution is not canonical T+1"
        )

    if event.event_type == "HANDOFF":
        return SourceDispatchResult(
            source_event_key=source_event_key(
                event
            ),
            status="IGNORED_SOURCE_HANDOFF",
            maturity_session=None,
        )

    if event.event_type != "ENTRY_OR_SCALE":
        raise DurableDispatcherError(
            f"unsupported source event type: {event.event_type}"
        )

    target = validate_frozen_target(
        event.target_sleeve
    )

    if target <= 0.0:
        raise DurableDispatcherError(
            "source ladder event must have positive target"
        )

    maturity = delayed_entry_session(
        sessions,
        event.execution_date,
    )

    key = source_event_key(
        event
    )

    # Persist first. If the process dies before starting WAIT_D40,
    # replaying this same causal source event repairs the state.
    durable = enqueue_delayed_event(
        con,
        source_event_key=key,
        cycle_id=str(
            event.cycle_id
        ),
        source_signal_session=event.signal_date,
        source_execution_session=event.execution_date,
        maturity_session=maturity,
        target=target,
        payload={
            "old_ath_date":
                event.old_ath_date,

            "old_ath_value":
                event.old_ath_value,

            "drawdown":
                event.drawdown,
        },
    )

    durable_status = str(
        durable[
            "status"
        ]
    )

    # Terminal durable record = duplicate historical/source replay.
    # Never reopen a completed/cancelled cycle.
    if durable_status in {
        "APPLIED",
        "CANCELLED",
    }:
        return SourceDispatchResult(
            source_event_key=key,
            status="ALREADY_RECORDED",
            maturity_session=maturity,
        )

    state = load_recovery_state(
        con
    )

    phase = str(
        state[
            "phase"
        ]
    )

    cycle_id = str(
        event.cycle_id
    )

    if phase == "NORMAL":
        transition_recovery_state(
            con,
            event_key=(
                f"cycle:{cycle_id}:"
                f"start:{event.signal_date}"
            ),
            to_phase="WAIT_D40",
            reason="START_SOURCE_CYCLE",
            updates={
                "cycle_id":
                    cycle_id,

                "old_ath":
                    float(
                        event.old_ath_value
                    ),

                "current_target":
                    0.0,

                "first_actual_entry_session":
                    None,

                "fixed_exit_session":
                    None,

                "old_ath_recovered":
                    0,
            },
            payload={
                "source_event_key":
                    key,

                "old_ath_date":
                    event.old_ath_date,
            },
        )

    elif phase in {
        "WAIT_D40",
        "RECOVERY_ACTIVE",
    }:
        if (
            str(
                state[
                    "cycle_id"
                ]
            )
            !=
            cycle_id
        ):
            raise DurableDispatcherError(
                "new source cycle overlaps active durable recovery cycle"
            )

        if abs(
            float(
                state[
                    "old_ath"
                ]
            )
            -
            float(
                event.old_ath_value
            )
        ) > TOL:
            raise DurableDispatcherError(
                "source event old ATH differs from durable cycle"
            )

    elif phase == "OLD_ATH_GUARD":
        raise DurableDispatcherError(
            "source event arrived before old-ATH guard release"
        )

    else:
        raise DurableDispatcherError(
            f"unexpected recovery phase: {phase}"
        )

    state = load_recovery_state(
        con
    )

    # If an event is sourced after the first actual entry but its D40
    # maturity is not strictly before H378, the frozen C2 rule discards it.
    if (
        state[
            "phase"
        ]
        ==
        "RECOVERY_ACTIVE"
    ):
        fixed_exit = str(
            state[
                "fixed_exit_session"
            ]
        )

        if (
            _position(
                sessions,
                maturity,
            )
            >=
            _position(
                sessions,
                fixed_exit,
            )
        ):
            set_delayed_event_status(
                con,
                source_event_key=key,
                to_status="CANCELLED",
            )

            return SourceDispatchResult(
                source_event_key=key,
                status="CANCELLED_OUTSIDE_H378",
                maturity_session=maturity,
            )

    return SourceDispatchResult(
        source_event_key=key,
        status="PENDING",
        maturity_session=maturity,
    )


def _recorded_transition(
    con: sqlite3.Connection,
    event_key: str,
) -> dict[str, Any] | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row

    try:
        row = con.execute(
            """
            SELECT *
            FROM recovery_transitions_v01
            WHERE event_key=?
            """,
            (
                event_key,
            ),
        ).fetchone()
    finally:
        con.row_factory = old_factory

    return (
        None
        if row is None
        else dict(row)
    )


def _resume_transition_then_apply(
    con: sqlite3.Connection,
    *,
    durable_event_key: str,
    transition_event_key: str,
) -> bool:
    """
    Recover the narrow crash window:

        durable event -> MATURED
        state transition -> committed
        process dies
        durable event still MATURED

    The transition journal is the idempotency authority.
    """
    recorded = _recorded_transition(
        con,
        transition_event_key,
    )

    if recorded is None:
        return False

    updates = json.loads(
        str(
            recorded[
                "updates_json"
            ]
        )
    )

    payload_text = recorded[
        "payload"
    ]

    payload = (
        None
        if payload_text is None
        else json.loads(
            str(
                payload_text
            )
        )
    )

    transition_recovery_state(
        con,
        event_key=transition_event_key,
        to_phase=str(
            recorded[
                "to_phase"
            ]
        ),
        reason=str(
            recorded[
                "reason"
            ]
        ),
        updates=updates,
        payload=payload,
    )

    set_delayed_event_status(
        con,
        source_event_key=durable_event_key,
        to_status="APPLIED",
    )

    return True


def _apply_due_event(
    con: sqlite3.Connection,
    *,
    row: dict[str, Any],
    session: str,
    sessions: Sequence[str],
) -> str:
    key = str(
        row[
            "source_event_key"
        ]
    )

    transition_key = (
        "apply:"
        +
        key
    )

    status = str(
        row[
            "status"
        ]
    )

    if status == "PENDING":
        set_delayed_event_status(
            con,
            source_event_key=key,
            to_status="MATURED",
        )

    elif status != "MATURED":
        raise DurableDispatcherError(
            f"due event has invalid durable status: {status}"
        )

    # Restart recovery after the economic transition already committed.
    if _resume_transition_then_apply(
        con,
        durable_event_key=key,
        transition_event_key=transition_key,
    ):
        return key

    state = load_recovery_state(
        con
    )

    cycle_id = str(
        row[
            "cycle_id"
        ]
    )

    if (
        str(
            state[
                "cycle_id"
            ]
        )
        !=
        cycle_id
    ):
        raise DurableDispatcherError(
            "due event cycle does not match durable state"
        )

    target = validate_frozen_target(
        float(
            row[
                "target"
            ]
        )
    )

    if state["phase"] == "WAIT_D40":
        fixed_exit = fixed_exit_session(
            sessions,
            session,
        )

        transition_recovery_state(
            con,
            event_key=transition_key,
            to_phase="RECOVERY_ACTIVE",
            reason="APPLY_FIRST_D40_ENTRY",
            updates={
                "current_target":
                    target,

                "first_actual_entry_session":
                    session,

                "fixed_exit_session":
                    fixed_exit,
            },
            payload={
                "source_event_key":
                    key,

                "maturity_session":
                    str(
                        row[
                            "maturity_session"
                        ]
                    ),
            },
        )

        # Frozen C2 keeps only delayed scale-ups strictly before H378.
        _cancel_open_events(
            con,
            cycle_id=cycle_id,
            at_or_after_session=fixed_exit,
            sessions=sessions,
        )

    elif state["phase"] == "RECOVERY_ACTIVE":
        fixed_exit = str(
            state[
                "fixed_exit_session"
            ]
        )

        if (
            _position(
                sessions,
                session,
            )
            >=
            _position(
                sessions,
                fixed_exit,
            )
        ):
            raise DurableDispatcherError(
                "D40 scale matured at/after frozen H378 exit"
            )

        current_target = float(
            state[
                "current_target"
            ]
        )

        if target <= current_target + TOL:
            raise DurableDispatcherError(
                "D40 scale must strictly increase frozen recovery target"
            )

        transition_recovery_state(
            con,
            event_key=transition_key,
            to_phase="RECOVERY_ACTIVE",
            reason="APPLY_D40_SCALE_UP",
            updates={
                "current_target":
                    target,
            },
            payload={
                "source_event_key":
                    key,

                "maturity_session":
                    str(
                        row[
                            "maturity_session"
                        ]
                    ),
            },
        )

    else:
        raise DurableDispatcherError(
            "D40 event cannot apply outside WAIT_D40/RECOVERY_ACTIVE"
        )

    set_delayed_event_status(
        con,
        source_event_key=key,
        to_status="APPLIED",
    )

    return key


def _rearm_after_fixed_exit_if_ready(
    con: sqlite3.Connection,
) -> bool:
    state = load_recovery_state(
        con
    )

    if (
        state[
            "phase"
        ]
        !=
        "OLD_ATH_GUARD"
        or
        int(
            state[
                "old_ath_recovered"
            ]
        )
        !=
        1
    ):
        return False

    cycle_id = str(
        state[
            "cycle_id"
        ]
    )

    fixed_exit = str(
        state[
            "fixed_exit_session"
        ]
    )

    transition_recovery_state(
        con,
        event_key=(
            f"cycle:{cycle_id}:"
            f"rearm-after-fixed-exit:{fixed_exit}"
        ),
        to_phase="NORMAL",
        reason="REARM_AFTER_FIXED_EXIT_OLD_ATH_ALREADY_RECOVERED",
        updates=_clear_cycle_updates(),
    )

    return True


def process_execution_session(
    con: sqlite3.Connection,
    *,
    session: str,
    sessions: Sequence[str],
) -> SessionDispatchResult:
    """
    Process actions that execute on the named canonical session.

    Missed D40/H378 sessions fail closed. A3B does NOT silently convert
    the frozen timing rule into a later execution rule.
    """
    current_position = _position(
        sessions,
        session,
    )

    state = load_recovery_state(
        con
    )

    if (
        state[
            "phase"
        ]
        ==
        "OLD_ATH_GUARD"
        and
        int(
            state[
                "old_ath_recovered"
            ]
        )
        ==
        1
    ):
        rearmed = _rearm_after_fixed_exit_if_ready(
            con
        )

        return SessionDispatchResult(
            session=session,
            applied_events=(),
            fixed_exit_applied=False,
            rearmed=rearmed,
        )

    state = load_recovery_state(
        con
    )

    # H378 execution has priority on its fixed session.
    if state["phase"] == "RECOVERY_ACTIVE":
        fixed_exit = str(
            state[
                "fixed_exit_session"
            ]
        )

        exit_position = _position(
            sessions,
            fixed_exit,
        )

        if current_position > exit_position:
            raise DurableDispatcherError(
                "frozen H378 execution session was missed"
            )

        if current_position == exit_position:
            cycle_id = str(
                state[
                    "cycle_id"
                ]
            )

            _cancel_open_events(
                con,
                cycle_id=cycle_id,
            )

            transition_recovery_state(
                con,
                event_key=(
                    f"cycle:{cycle_id}:"
                    f"fixed-exit:{fixed_exit}"
                ),
                to_phase="OLD_ATH_GUARD",
                reason="H378_FIXED_EXIT",
                updates={
                    "current_target":
                        0.0,
                },
            )

            rearmed = _rearm_after_fixed_exit_if_ready(
                con
            )

            return SessionDispatchResult(
                session=session,
                applied_events=(),
                fixed_exit_applied=True,
                rearmed=rearmed,
            )

    state = load_recovery_state(
        con
    )

    if state["phase"] == "NORMAL":
        orphan = pending_events(
            con
        )

        if orphan:
            raise DurableDispatcherError(
                "orphan pending D40 event while durable state is NORMAL"
            )

        return SessionDispatchResult(
            session=session,
            applied_events=(),
            fixed_exit_applied=False,
            rearmed=False,
        )

    if state["phase"] not in {
        "WAIT_D40",
        "RECOVERY_ACTIVE",
        "OLD_ATH_GUARD",
    }:
        raise DurableDispatcherError(
            f"unexpected durable phase: {state['phase']}"
        )

    if state["phase"] == "OLD_ATH_GUARD":
        return SessionDispatchResult(
            session=session,
            applied_events=(),
            fixed_exit_applied=False,
            rearmed=False,
        )

    cycle_id = str(
        state[
            "cycle_id"
        ]
    )

    open_events = pending_events(
        con,
        cycle_id=cycle_id,
    )

    due: list[
        dict[str, Any]
    ] = []

    for row in open_events:
        maturity = str(
            row[
                "maturity_session"
            ]
        )

        maturity_position = _position(
            sessions,
            maturity,
        )

        if maturity_position < current_position:
            raise DurableDispatcherError(
                "frozen D40 maturity session was missed"
            )

        if maturity_position == current_position:
            due.append(
                row
            )

    applied: list[str] = []

    for row in due:
        applied.append(
            _apply_due_event(
                con,
                row=row,
                session=session,
                sessions=sessions,
            )
        )

    validate_recovery_state(
        con
    )

    return SessionDispatchResult(
        session=session,
        applied_events=tuple(
            applied
        ),
        fixed_exit_applied=False,
        rearmed=False,
    )


def observe_completed_close(
    con: sqlite3.Connection,
    *,
    session: str,
    close: float,
) -> CloseDispatchResult:
    """
    Observe the completed IVV close after execution-session actions.

    This ordering enforces the frozen STRICTLY BEFORE cancellation rule:
    if old ATH recovery occurs on the same session as first D40 maturity,
    the D40 event has already applied and the cycle is NOT cancelled.
    """
    price = float(
        close
    )

    if price <= 0.0:
        raise DurableDispatcherError(
            "completed close must be positive"
        )

    state = load_recovery_state(
        con
    )

    phase = str(
        state[
            "phase"
        ]
    )

    if phase == "NORMAL":
        return CloseDispatchResult(
            session=session,
            status="NO_ACTIVE_CYCLE",
        )

    old_ath = float(
        state[
            "old_ath"
        ]
    )

    if price + TOL < old_ath:
        return CloseDispatchResult(
            session=session,
            status="OLD_ATH_NOT_RECOVERED",
        )

    cycle_id = str(
        state[
            "cycle_id"
        ]
    )

    if phase == "WAIT_D40":
        _cancel_open_events(
            con,
            cycle_id=cycle_id,
        )

        transition_recovery_state(
            con,
            event_key=(
                f"cycle:{cycle_id}:"
                f"cancel-pre-entry:{session}"
            ),
            to_phase="NORMAL",
            reason="OLD_ATH_RECOVERED_STRICTLY_BEFORE_FIRST_D40_ENTRY",
            updates=_clear_cycle_updates(),
        )

        return CloseDispatchResult(
            session=session,
            status="CANCELLED_BEFORE_FIRST_ENTRY",
        )

    if phase == "RECOVERY_ACTIVE":
        if int(
            state[
                "old_ath_recovered"
            ]
        ) == 0:
            transition_recovery_state(
                con,
                event_key=(
                    f"cycle:{cycle_id}:"
                    f"old-ath-recovered:{session}"
                ),
                to_phase="RECOVERY_ACTIVE",
                reason="OLD_ATH_RECOVERED_WHILE_H378_ACTIVE",
                updates={
                    "old_ath_recovered":
                        1,
                },
            )

            return CloseDispatchResult(
                session=session,
                status="OLD_ATH_RECOVERY_RECORDED",
            )

        return CloseDispatchResult(
            session=session,
            status="OLD_ATH_ALREADY_RECORDED",
        )

    if phase == "OLD_ATH_GUARD":
        transition_recovery_state(
            con,
            event_key=(
                f"cycle:{cycle_id}:"
                f"rearm-old-ath:{session}"
            ),
            to_phase="NORMAL",
            reason="REARM_AFTER_OLD_ATH",
            updates=_clear_cycle_updates(),
        )

        return CloseDispatchResult(
            session=session,
            status="REARMED_AFTER_OLD_ATH",
        )

    raise DurableDispatcherError(
        f"unexpected durable phase: {phase}"
    )


def replay_compiled_history_to_store(
    con: sqlite3.Connection,
    *,
    rows: Sequence[RecoveryInputRow],
) -> dict[str, Any]:
    """
    End-to-end historical semantic replay.

    Source events come from the causal A3 compiler.
    Final C2 dates are never used as dispatcher inputs.
    """
    initialize_recovery_state(
        con
    )

    sessions = canonical_sessions(
        rows
    )

    compiled_sources = source_events(
        compile_source_cycles(
            rows
        )
    )

    by_signal: dict[
        str,
        list[SourceEvent],
    ] = {}

    for event in compiled_sources:
        by_signal.setdefault(
            event.signal_date,
            [],
        ).append(
            event
        )

    for row in rows:
        process_execution_session(
            con,
            session=row.date,
            sessions=sessions,
        )

        for event in by_signal.get(
            row.date,
            [],
        ):
            ingest_source_event(
                con,
                event=event,
                observed_session=row.date,
                sessions=sessions,
            )

        observe_completed_close(
            con,
            session=row.date,
            close=row.close,
        )

    return validate_recovery_state(
        con
    )
