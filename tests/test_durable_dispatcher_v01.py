from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from sp1execution.recovery.causal_compiler_v01 import (
    RecoveryInputRow,
    SourceEvent,
    compile_final_schedule,
    load_canonical_ivv_rows,
)
from sp1execution.recovery.durable_dispatcher_v01 import (
    DurableDispatcherError,
    ingest_source_event,
    observe_completed_close,
    process_execution_session,
    replay_compiled_history_to_store,
)
from sp1execution.recovery.state_v01 import (
    initialize_recovery_state,
    load_recovery_state,
)


ROOT = Path(__file__).resolve().parents[1]

INPUT = (
    ROOT
    / "contracts/research/"
      "phase_b0_canonical_sp2_ivv_path_v0.1.csv"
)


def _memory_db():
    con = sqlite3.connect(
        ":memory:"
    )

    con.row_factory = sqlite3.Row

    initialize_recovery_state(
        con
    )

    return con


def _sessions(
    count: int = 600,
):
    return tuple(
        f"S{i:03d}"
        for i in range(
            count
        )
    )


def _source(
    *,
    signal="S005",
    execution="S006",
    cycle=1,
    target=0.10,
    old_ath=100.0,
):
    return SourceEvent(
        cycle_id=cycle,
        event_type="ENTRY_OR_SCALE",
        signal_date=signal,
        execution_date=execution,
        old_ath_date="S000",
        old_ath_value=old_ath,
        drawdown=0.30,
        target_sleeve=target,
    )


def test_source_event_becomes_durable_pending_d40():
    con = _memory_db()

    result = ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=_sessions(),
    )

    assert result.status == "PENDING"
    assert result.maturity_session == "S046"

    state = load_recovery_state(
        con
    )

    assert state["phase"] == "WAIT_D40"
    assert state["cycle_id"] == "1"

    row = con.execute(
        """
        SELECT *
        FROM recovery_pending_events_v01
        """
    ).fetchone()

    assert row["status"] == "PENDING"
    assert row["maturity_session"] == "S046"


def test_source_handoff_is_segmentation_only():
    con = _memory_db()

    event = SourceEvent(
        cycle_id=1,
        event_type="HANDOFF",
        signal_date="S010",
        execution_date="S011",
        old_ath_date="S000",
        old_ath_value=100.0,
        drawdown=0.15,
        target_sleeve=0.0,
    )

    result = ingest_source_event(
        con,
        event=event,
        observed_session="S010",
        sessions=_sessions(),
    )

    assert (
        result.status
        ==
        "IGNORED_SOURCE_HANDOFF"
    )

    assert con.execute(
        """
        SELECT COUNT(*)
        FROM recovery_pending_events_v01
        """
    ).fetchone()[0] == 0


def test_d40_pending_matured_applied_and_h378_clock():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    result = process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    assert len(
        result.applied_events
    ) == 1

    state = load_recovery_state(
        con
    )

    assert state["phase"] == "RECOVERY_ACTIVE"
    assert state["current_target"] == 0.10
    assert state["first_actual_entry_session"] == "S046"
    assert state["fixed_exit_session"] == "S424"

    row = con.execute(
        """
        SELECT status
        FROM recovery_pending_events_v01
        """
    ).fetchone()

    assert row["status"] == "APPLIED"


def test_scale_up_does_not_reset_h378():
    con = _memory_db()
    sessions = _sessions()

    first = _source()

    second = _source(
        signal="S010",
        execution="S011",
        target=0.30,
    )

    ingest_source_event(
        con,
        event=first,
        observed_session="S005",
        sessions=sessions,
    )

    ingest_source_event(
        con,
        event=second,
        observed_session="S010",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    before = load_recovery_state(
        con
    )

    process_execution_session(
        con,
        session="S051",
        sessions=sessions,
    )

    after = load_recovery_state(
        con
    )

    assert before["fixed_exit_session"] == "S424"
    assert after["fixed_exit_session"] == "S424"
    assert after["first_actual_entry_session"] == "S046"
    assert after["current_target"] == 0.30


def test_h378_fixed_exit_enters_guard():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    result = process_execution_session(
        con,
        session="S424",
        sessions=sessions,
    )

    assert result.fixed_exit_applied is True

    state = load_recovery_state(
        con
    )

    assert state["phase"] == "OLD_ATH_GUARD"
    assert state["current_target"] == 0.0


def test_old_ath_strictly_before_first_entry_cancels():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    result = observe_completed_close(
        con,
        session="S008",
        close=100.0,
    )

    assert (
        result.status
        ==
        "CANCELLED_BEFORE_FIRST_ENTRY"
    )

    state = load_recovery_state(
        con
    )

    assert state["phase"] == "NORMAL"

    row = con.execute(
        """
        SELECT status
        FROM recovery_pending_events_v01
        """
    ).fetchone()

    assert row["status"] == "CANCELLED"


def test_old_ath_same_session_as_first_entry_does_not_cancel():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    result = observe_completed_close(
        con,
        session="S046",
        close=100.0,
    )

    assert (
        result.status
        ==
        "OLD_ATH_RECOVERY_RECORDED"
    )

    state = load_recovery_state(
        con
    )

    assert state["phase"] == "RECOVERY_ACTIVE"
    assert state["old_ath_recovered"] == 1


def test_old_ath_recovery_during_active_rearms_at_fixed_exit():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    observe_completed_close(
        con,
        session="S100",
        close=100.0,
    )

    result = process_execution_session(
        con,
        session="S424",
        sessions=sessions,
    )

    assert result.fixed_exit_applied is True
    assert result.rearmed is True

    assert (
        load_recovery_state(
            con
        )["phase"]
        ==
        "NORMAL"
    )


def test_guard_waits_for_old_ath_if_exit_occurs_first():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S424",
        sessions=sessions,
    )

    assert (
        load_recovery_state(
            con
        )["phase"]
        ==
        "OLD_ATH_GUARD"
    )

    result = observe_completed_close(
        con,
        session="S450",
        close=100.0,
    )

    assert (
        result.status
        ==
        "REARMED_AFTER_OLD_ATH"
    )

    assert (
        load_recovery_state(
            con
        )["phase"]
        ==
        "NORMAL"
    )


def test_missed_d40_fails_closed_instead_of_late_execution():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    with pytest.raises(
        DurableDispatcherError,
        match="D40 maturity session was missed",
    ):
        process_execution_session(
            con,
            session="S047",
            sessions=sessions,
        )

    state = load_recovery_state(
        con
    )

    assert state["phase"] == "WAIT_D40"
    assert state["current_target"] == 0.0


def test_missed_h378_fails_closed():
    con = _memory_db()
    sessions = _sessions()

    ingest_source_event(
        con,
        event=_source(),
        observed_session="S005",
        sessions=sessions,
    )

    process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    with pytest.raises(
        DurableDispatcherError,
        match="H378 execution session was missed",
    ):
        process_execution_session(
            con,
            session="S425",
            sessions=sessions,
        )


def test_restart_preserves_pending_event_and_applies_once():
    sessions = _sessions()

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.sqlite"

        con = sqlite3.connect(
            db
        )

        con.row_factory = sqlite3.Row

        initialize_recovery_state(
            con
        )

        ingest_source_event(
            con,
            event=_source(),
            observed_session="S005",
            sessions=sessions,
        )

        con.close()

        con = sqlite3.connect(
            db
        )

        con.row_factory = sqlite3.Row

        initialize_recovery_state(
            con
        )

        process_execution_session(
            con,
            session="S046",
            sessions=sessions,
        )

        revision_after_first = int(
            load_recovery_state(
                con
            )["revision"]
        )

        process_execution_session(
            con,
            session="S046",
            sessions=sessions,
        )

        revision_after_replay = int(
            load_recovery_state(
                con
            )["revision"]
        )

        assert (
            revision_after_replay
            ==
            revision_after_first
        )

        assert con.execute(
            """
            SELECT COUNT(*)
            FROM recovery_transitions_v01
            WHERE reason='APPLY_FIRST_D40_ENTRY'
            """
        ).fetchone()[0] == 1

        con.close()


def test_restart_after_transition_before_applied_status_recovers():
    con = _memory_db()
    sessions = _sessions()

    event = _source()

    ingest_source_event(
        con,
        event=event,
        observed_session="S005",
        sessions=sessions,
    )

    key = (
        f"cycle:{event.cycle_id}:"
        f"source:{event.signal_date}:"
        f"{event.execution_date}:"
        f"{event.target_sleeve:.2f}"
    )

    # Simulate the narrow crash window manually:
    # durable status becomes MATURED and economic transition commits,
    # but APPLIED durable status is not written.
    from sp1execution.recovery.state_v01 import (
        set_delayed_event_status,
        transition_recovery_state,
    )

    set_delayed_event_status(
        con,
        source_event_key=key,
        to_status="MATURED",
    )

    transition_recovery_state(
        con,
        event_key="apply:" + key,
        to_phase="RECOVERY_ACTIVE",
        reason="APPLY_FIRST_D40_ENTRY",
        updates={
            "current_target": 0.10,
            "first_actual_entry_session": "S046",
            "fixed_exit_session": "S424",
        },
        payload={
            "source_event_key": key,
            "maturity_session": "S046",
        },
    )

    result = process_execution_session(
        con,
        session="S046",
        sessions=sessions,
    )

    assert result.applied_events == (
        key,
    )

    row = con.execute(
        """
        SELECT status
        FROM recovery_pending_events_v01
        WHERE source_event_key=?
        """,
        (
            key,
        ),
    ).fetchone()

    assert row["status"] == "APPLIED"

    assert con.execute(
        """
        SELECT COUNT(*)
        FROM recovery_transitions_v01
        WHERE event_key=?
        """,
        (
            "apply:" + key,
        ),
    ).fetchone()[0] == 1


def test_full_historical_durable_replay_reaches_exact_c2_schedule():
    rows = load_canonical_ivv_rows(
        INPUT
    )

    con = _memory_db()

    final_state = replay_compiled_history_to_store(
        con,
        rows=rows,
    )

    assert final_state["phase"] == "NORMAL"
    assert final_state["current_target"] == 0.0
    assert final_state["reserve_bucket_eur"] == 0.0

    durable = con.execute(
        """
        SELECT
            source_signal_session,
            source_execution_session,
            maturity_session,
            target,
            status
        FROM recovery_pending_events_v01
        ORDER BY
            maturity_session,
            id
        """
    ).fetchall()

    assert len(durable) == 7
    assert all(
        row["status"] == "APPLIED"
        for row in durable
    )

    expected = [
        row
        for row in compile_final_schedule(
            rows
        )
        if row.event_type
        ==
        "DELAYED_ENTRY_OR_SCALE"
    ]

    assert [
        (
            row["source_signal_session"],
            row["source_execution_session"],
            row["maturity_session"],
            float(
                row["target"]
            ),
        )
        for row in durable
    ] == [
        (
            row.source_signal_date,
            row.source_execution_t_plus_1,
            row.final_execution_date,
            row.target_sleeve,
        )
        for row in expected
    ]


def test_full_historical_transition_inventory_and_final_revision():
    rows = load_canonical_ivv_rows(
        INPUT
    )

    con = _memory_db()

    replay_compiled_history_to_store(
        con,
        rows=rows,
    )

    reasons = [
        row[0]
        for row in con.execute(
            """
            SELECT reason
            FROM recovery_transitions_v01
            ORDER BY id
            """
        ).fetchall()
    ]

    assert reasons.count(
        "START_SOURCE_CYCLE"
    ) == 3

    assert reasons.count(
        "APPLY_FIRST_D40_ENTRY"
    ) == 3

    assert reasons.count(
        "APPLY_D40_SCALE_UP"
    ) == 4

    assert reasons.count(
        "H378_FIXED_EXIT"
    ) == 3

    assert reasons.count(
        "REARM_AFTER_OLD_ATH"
    ) == 2

    assert reasons.count(
        "OLD_ATH_RECOVERED_WHILE_H378_ACTIVE"
    ) == 1

    assert reasons.count(
        "REARM_AFTER_FIXED_EXIT_OLD_ATH_ALREADY_RECOVERED"
    ) == 1

    assert len(
        reasons
    ) == 17

    assert int(
        load_recovery_state(
            con
        )["revision"]
    ) == 18


def test_full_historical_fixed_exit_transitions_are_exact():
    rows = load_canonical_ivv_rows(
        INPUT
    )

    con = _memory_db()

    replay_compiled_history_to_store(
        con,
        rows=rows,
    )

    exit_keys = [
        row[0]
        for row in con.execute(
            """
            SELECT event_key
            FROM recovery_transitions_v01
            WHERE reason='H378_FIXED_EXIT'
            ORDER BY id
            """
        ).fetchall()
    ]

    assert exit_keys == [
        "cycle:1:fixed-exit:2004-03-09",
        "cycle:2:fixed-exit:2010-06-07",
        "cycle:3:fixed-exit:2021-11-16",
    ]


def test_full_history_has_no_open_d40_events_after_replay():
    rows = load_canonical_ivv_rows(
        INPUT
    )

    con = _memory_db()

    replay_compiled_history_to_store(
        con,
        rows=rows,
    )

    assert con.execute(
        """
        SELECT COUNT(*)
        FROM recovery_pending_events_v01
        WHERE status IN ('PENDING','MATURED')
        """
    ).fetchone()[0] == 0
