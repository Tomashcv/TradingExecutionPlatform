from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from sp1execution.recovery.core_return_v01 import (
    ENTRY_DELAY_US_TRADING_INTERVALS,
    FIXED_HOLD_US_TRADING_INTERVALS,
    FROZEN_LADDER,
    delayed_entry_session,
    fixed_exit_session,
    target_from_drawdown,
)
from sp1execution.strategy.robust import (
    HANDOFF_RECOVERY,
    LEVELS,
    Mode,
    RobustState,
    drawdown_fraction,
)


SOURCE_HANDOFF_RECOVERY = 0.55
TOL = 1e-12


class CausalCompilerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryInputRow:
    date: str
    close: float


@dataclass(frozen=True)
class SourceEvent:
    cycle_id: int
    event_type: str
    signal_date: str
    execution_date: str
    old_ath_date: str
    old_ath_value: float
    drawdown: float
    target_sleeve: float


@dataclass(frozen=True)
class SourceCycle:
    cycle_id: int
    old_ath_date: str
    old_ath_value: float
    events: tuple[SourceEvent, ...]
    old_ath_recovery_date: str | None


@dataclass(frozen=True)
class FinalEvent:
    cycle_id: int
    old_ath_date: str
    source_signal_date: str | None
    source_execution_t_plus_1: str | None
    final_execution_date: str
    event_type: str
    target_sleeve: float
    delay_td: int
    hold_td: int
    fixed_exit_signal_date: str | None


@dataclass(frozen=True)
class GuardAuditRow:
    cycle_id: int
    fixed_exit: str
    old_ath_recovery_date: str | None
    effective_no_new_cycle_until: str | None
    next_cycle_id: int | None
    next_source_signal: str | None
    no_reentry_before_guard_release: bool


@dataclass
class _CycleBuilder:
    cycle_id: int
    old_ath_date: str
    old_ath_value: float
    events: list[SourceEvent]
    old_ath_recovery_date: str | None = None


def assert_source_contract_compatible() -> None:
    normalized_levels = tuple(
        (
            float(threshold),
            float(target),
        )
        for threshold, target in LEVELS
    )

    if normalized_levels != FROZEN_LADDER:
        raise CausalCompilerError(
            "legacy source ladder differs from frozen CORE_RETURN ladder"
        )

    if abs(
        float(HANDOFF_RECOVERY)
        -
        SOURCE_HANDOFF_RECOVERY
    ) > TOL:
        raise CausalCompilerError(
            "legacy source handoff differs from frozen 55% segmentation rule"
        )


def load_canonical_ivv_rows(
    path: str | Path,
) -> tuple[RecoveryInputRow, ...]:
    source = Path(path)

    with source.open(
        newline="",
        encoding="utf-8",
    ) as fh:
        reader = csv.DictReader(fh)

        required = {
            "date",
            "ivv_nav",
        }

        if not required.issubset(
            set(reader.fieldnames or ())
        ):
            raise CausalCompilerError(
                "canonical IVV input missing date/ivv_nav"
            )

        rows = tuple(
            RecoveryInputRow(
                date=str(row["date"]),
                close=float(row["ivv_nav"]),
            )
            for row in reader
        )

    validate_input_rows(
        rows
    )

    return rows


def validate_input_rows(
    rows: Sequence[RecoveryInputRow],
) -> None:
    if not rows:
        raise CausalCompilerError(
            "empty IVV history"
        )

    dates = [
        row.date
        for row in rows
    ]

    if dates != sorted(dates):
        raise CausalCompilerError(
            "IVV sessions are not monotonic"
        )

    if len(set(dates)) != len(dates):
        raise CausalCompilerError(
            "duplicate IVV sessions"
        )

    for row in rows:
        if row.close <= 0.0:
            raise CausalCompilerError(
                "non-positive IVV close"
            )


def canonical_sessions(
    rows: Sequence[RecoveryInputRow],
) -> tuple[str, ...]:
    validate_input_rows(
        rows
    )

    return tuple(
        row.date
        for row in rows
    )


def session_position_map(
    sessions: Sequence[str],
) -> dict[str, int]:
    return {
        session: index
        for index, session
        in enumerate(sessions)
    }


def next_trading_session(
    sessions: Sequence[str],
    signal_session: str,
) -> str:
    position = session_position_map(
        sessions
    )

    if signal_session not in position:
        raise CausalCompilerError(
            f"signal session missing from calendar: {signal_session}"
        )

    index = position[
        signal_session
    ] + 1

    if index >= len(sessions):
        raise CausalCompilerError(
            "calendar ends before source T+1"
        )

    return str(
        sessions[index]
    )


def previous_trading_session(
    sessions: Sequence[str],
    execution_session: str,
) -> str:
    position = session_position_map(
        sessions
    )

    if execution_session not in position:
        raise CausalCompilerError(
            f"execution session missing from calendar: {execution_session}"
        )

    index = position[
        execution_session
    ] - 1

    if index < 0:
        raise CausalCompilerError(
            "calendar begins at fixed exit"
        )

    return str(
        sessions[index]
    )


def compile_source_cycles(
    rows: Sequence[RecoveryInputRow],
) -> tuple[SourceCycle, ...]:
    """
    Reproduce the original causal ROBUST source state.

    Important:
    - ladder events remain source events;
    - the 55% recovery handoff SEGMENTS the source cycle only;
    - the handoff is NOT the final CORE_RETURN exit;
    - rearm occurs only at the old ATH.
    """
    assert_source_contract_compatible()

    validate_input_rows(
        rows
    )

    sessions = canonical_sessions(
        rows
    )

    state = RobustState()

    running_ath_value: float | None = None
    running_ath_date: str | None = None

    active: _CycleBuilder | None = None

    completed: list[_CycleBuilder] = []

    cycle_counter = 0

    for index, row in enumerate(rows):
        price = float(
            row.close
        )

        pre_mode = state.mode

        if (
            running_ath_value is None
            or
            price > running_ath_value
        ):
            running_ath_value = price
            running_ath_date = row.date

        decision = state.observe_close(
            price
        )

        event_text = str(
            decision.event
        )

        if event_text.startswith(
            "ROTATE_DD_"
        ):
            if pre_mode == Mode.NORMAL:
                if active is not None:
                    raise CausalCompilerError(
                        "new source cycle while previous builder remains active"
                    )

                if (
                    state.old_peak is None
                    or
                    running_ath_date is None
                ):
                    raise CausalCompilerError(
                        "source cycle missing causal ATH"
                    )

                cycle_counter += 1

                active = _CycleBuilder(
                    cycle_id=cycle_counter,
                    old_ath_date=running_ath_date,
                    old_ath_value=float(
                        state.old_peak
                    ),
                    events=[],
                )

            if active is None:
                raise CausalCompilerError(
                    "scale event without active source cycle"
                )

            if index + 1 >= len(sessions):
                raise CausalCompilerError(
                    "source ladder event lacks T+1 session"
                )

            execution_date = str(
                sessions[
                    index + 1
                ]
            )

            if state.old_peak is None:
                raise CausalCompilerError(
                    "source event missing old peak"
                )

            dd = drawdown_fraction(
                price,
                float(
                    state.old_peak
                ),
            )

            expected_target = target_from_drawdown(
                dd
            )

            actual_target = float(
                decision.target_sp500
            )

            if abs(
                actual_target
                -
                expected_target
            ) > TOL:
                raise CausalCompilerError(
                    "source target differs from frozen ladder"
                )

            active.events.append(
                SourceEvent(
                    cycle_id=active.cycle_id,
                    event_type="ENTRY_OR_SCALE",
                    signal_date=row.date,
                    execution_date=execution_date,
                    old_ath_date=active.old_ath_date,
                    old_ath_value=active.old_ath_value,
                    drawdown=dd,
                    target_sleeve=actual_target,
                )
            )

        elif event_text.startswith(
            "HANDOFF_REC_"
        ):
            if active is None:
                raise CausalCompilerError(
                    "source handoff without active cycle"
                )

            if index + 1 >= len(sessions):
                raise CausalCompilerError(
                    "source handoff lacks T+1 session"
                )

            if state.old_peak is None:
                raise CausalCompilerError(
                    "source handoff missing old peak"
                )

            dd = drawdown_fraction(
                price,
                float(
                    state.old_peak
                ),
            )

            active.events.append(
                SourceEvent(
                    cycle_id=active.cycle_id,
                    event_type="HANDOFF",
                    signal_date=row.date,
                    execution_date=str(
                        sessions[
                            index + 1
                        ]
                    ),
                    old_ath_date=active.old_ath_date,
                    old_ath_value=active.old_ath_value,
                    drawdown=dd,
                    target_sleeve=0.0,
                )
            )

        elif event_text == "REARM_AFTER_OLD_ATH":
            if active is None:
                raise CausalCompilerError(
                    "old-ATH rearm without active source cycle"
                )

            active.old_ath_recovery_date = row.date

            completed.append(
                active
            )

            active = None

            if (
                running_ath_value is None
                or
                price > running_ath_value
            ):
                running_ath_value = price
                running_ath_date = row.date

        elif event_text != "NO_ACTION":
            raise CausalCompilerError(
                f"unknown legacy source event: {event_text}"
            )

    if active is not None:
        completed.append(
            active
        )

    out = tuple(
        SourceCycle(
            cycle_id=cycle.cycle_id,
            old_ath_date=cycle.old_ath_date,
            old_ath_value=cycle.old_ath_value,
            events=tuple(
                cycle.events
            ),
            old_ath_recovery_date=cycle.old_ath_recovery_date,
        )
        for cycle in completed
    )

    return out


def source_events(
    cycles: Sequence[SourceCycle],
) -> tuple[SourceEvent, ...]:
    return tuple(
        event
        for cycle in cycles
        for event in cycle.events
    )


def compile_final_schedule(
    rows: Sequence[RecoveryInputRow],
) -> tuple[FinalEvent, ...]:
    """
    Compile frozen D40/H378 CORE_RETURN events from causal source cycles.

    D40:
        source execution T+1 + 40 canonical US trading intervals.

    H378:
        first actual delayed entry + 378 canonical US trading intervals.

    A later scale-up never resets H378.

    Old-ATH recovery strictly before first delayed entry cancels the cycle.
    """
    cycles = compile_source_cycles(
        rows
    )

    sessions = canonical_sessions(
        rows
    )

    position = session_position_map(
        sessions
    )

    output: list[FinalEvent] = []

    for cycle in cycles:
        entries = [
            event
            for event in cycle.events
            if event.event_type
            ==
            "ENTRY_OR_SCALE"
        ]

        if not entries:
            continue

        delayed: list[
            tuple[SourceEvent, str]
        ] = []

        for event in entries:
            execution = delayed_entry_session(
                sessions,
                event.execution_date,
            )

            delayed.append(
                (
                    event,
                    execution,
                )
            )

        delayed.sort(
            key=lambda pair:
                position[
                    pair[1]
                ]
        )

        first_delayed = delayed[
            0
        ][1]

        cancelled = bool(
            cycle.old_ath_recovery_date
            is not None
            and
            position[
                cycle.old_ath_recovery_date
            ]
            <
            position[
                first_delayed
            ]
        )

        if cancelled:
            continue

        fixed_exit = fixed_exit_session(
            sessions,
            first_delayed,
        )

        fixed_exit_position = position[
            fixed_exit
        ]

        for event, execution in delayed:
            if (
                position[
                    execution
                ]
                >=
                fixed_exit_position
            ):
                continue

            output.append(
                FinalEvent(
                    cycle_id=cycle.cycle_id,
                    old_ath_date=cycle.old_ath_date,
                    source_signal_date=event.signal_date,
                    source_execution_t_plus_1=event.execution_date,
                    final_execution_date=execution,
                    event_type="DELAYED_ENTRY_OR_SCALE",
                    target_sleeve=event.target_sleeve,
                    delay_td=ENTRY_DELAY_US_TRADING_INTERVALS,
                    hold_td=FIXED_HOLD_US_TRADING_INTERVALS,
                    fixed_exit_signal_date=None,
                )
            )

        output.append(
            FinalEvent(
                cycle_id=cycle.cycle_id,
                old_ath_date=cycle.old_ath_date,
                source_signal_date=None,
                source_execution_t_plus_1=None,
                final_execution_date=fixed_exit,
                event_type="FIXED_EXIT",
                target_sleeve=0.0,
                delay_td=ENTRY_DELAY_US_TRADING_INTERVALS,
                hold_td=FIXED_HOLD_US_TRADING_INTERVALS,
                fixed_exit_signal_date=previous_trading_session(
                    sessions,
                    fixed_exit,
                ),
            )
        )

    output.sort(
        key=lambda event:
            (
                event.cycle_id,
                position[
                    event.final_execution_date
                ],
            )
    )

    return tuple(
        output
    )


def compile_guard_audit(
    rows: Sequence[RecoveryInputRow],
) -> tuple[GuardAuditRow, ...]:
    cycles = compile_source_cycles(
        rows
    )

    schedule = compile_final_schedule(
        rows
    )

    position = session_position_map(
        canonical_sessions(
            rows
        )
    )

    by_cycle: dict[
        int,
        list[FinalEvent],
    ] = {}

    for event in schedule:
        by_cycle.setdefault(
            event.cycle_id,
            [],
        ).append(
            event
        )

    active_cycles = [
        cycle
        for cycle in cycles
        if cycle.cycle_id
        in by_cycle
    ]

    out: list[GuardAuditRow] = []

    for index, cycle in enumerate(
        active_cycles
    ):
        events = by_cycle[
            cycle.cycle_id
        ]

        exits = [
            event
            for event in events
            if event.event_type
            ==
            "FIXED_EXIT"
        ]

        if len(exits) != 1:
            raise CausalCompilerError(
                "expected exactly one fixed exit per active cycle"
            )

        fixed_exit = exits[
            0
        ].final_execution_date

        recovery = cycle.old_ath_recovery_date

        if recovery is None:
            effective = fixed_exit
        else:
            effective = (
                recovery
                if position[
                    recovery
                ]
                >
                position[
                    fixed_exit
                ]
                else fixed_exit
            )

        next_cycle_id: int | None = None
        next_source_signal: str | None = None

        if index + 1 < len(
            active_cycles
        ):
            nxt = active_cycles[
                index + 1
            ]

            next_entries = [
                event
                for event in nxt.events
                if event.event_type
                ==
                "ENTRY_OR_SCALE"
            ]

            if not next_entries:
                raise CausalCompilerError(
                    "next source cycle has no entry signal"
                )

            next_cycle_id = nxt.cycle_id

            next_source_signal = min(
                (
                    event.signal_date
                    for event
                    in next_entries
                ),
                key=lambda session:
                    position[
                        session
                    ],
            )

        no_reentry = bool(
            next_source_signal is None
            or
            position[
                next_source_signal
            ]
            >
            position[
                effective
            ]
        )

        out.append(
            GuardAuditRow(
                cycle_id=cycle.cycle_id,
                fixed_exit=fixed_exit,
                old_ath_recovery_date=recovery,
                effective_no_new_cycle_until=effective,
                next_cycle_id=next_cycle_id,
                next_source_signal=next_source_signal,
                no_reentry_before_guard_release=no_reentry,
            )
        )

    return tuple(
        out
    )


def trading_interval_distance(
    rows: Sequence[RecoveryInputRow],
    start: str,
    end: str,
) -> int:
    position = session_position_map(
        canonical_sessions(
            rows
        )
    )

    try:
        return (
            position[
                end
            ]
            -
            position[
                start
            ]
        )
    except KeyError as exc:
        raise CausalCompilerError(
            "interval endpoint missing from canonical calendar"
        ) from exc
