import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sp1execution.recovery.state_v01 import (
    RecoveryStateError,
    apply_reserve_ledger_event,
    enqueue_delayed_event,
    ensure_recovery_schema,
    initialize_recovery_state,
    load_recovery_state,
    pending_events,
    set_delayed_event_status,
    transition_recovery_state,
    validate_recovery_state,
)
from sp1execution.state.v04_store import (
    ensure_schema as ensure_v04_schema,
)


ROOT = Path(__file__).resolve().parents[1]


PROVENANCE_HASHES = {
    (
        ROOT
        / "contracts/research/"
        "phase_c4b3d_realistic_policy_decision_v0.1.csv"
    ):
        "009a09cb3d4778d7fc954f1b929b78dce"
        "bf2ce043b6e1762e556ef9c62ae143f",

    (
        ROOT
        / "contracts/research/"
        "phase_c4b3d_summary_v0.1.json"
    ):
        "6d2b1ccc5d2fdd686456ce9f83e5f69"
        "e8e3e4cd76b31d6d72627ea3ca5c7e89e",

    (
        ROOT
        / "contracts/research/"
        "phase_c4c0_physical_execution_architecture_v0.1.json"
    ):
        "5a9da7c42f2ee422908ab8e6887ad9f2"
        "d5771f1f3016da7e2fb32da56995b30c",

    (
        ROOT
        / "contracts/research/"
        "phase_c4c0_summary_v0.1.json"
    ):
        "dce23cdf42fd23e5e7a686011a8c3ce60"
        "d902b68063bcb7c20daae09d20d4ee4",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def _db():
    con = sqlite3.connect(
        ":memory:"
    )

    con.row_factory = sqlite3.Row

    ensure_recovery_schema(
        con
    )

    initialize_recovery_state(
        con
    )

    return con


def _activate(
    con,
    *,
    cycle_id="C1",
    old_ath=100.0,
    target=0.30,
):
    transition_recovery_state(
        con,
        event_key=f"{cycle_id}:start",
        to_phase="WAIT_D40",
        reason="START",
        updates={
            "cycle_id":
                cycle_id,

            "old_ath":
                old_ath,
        },
    )

    transition_recovery_state(
        con,
        event_key=f"{cycle_id}:entry",
        to_phase="RECOVERY_ACTIVE",
        reason="D40_MATURED",
        updates={
            "current_target":
                target,

            "first_actual_entry_session":
                "S010",

            "fixed_exit_session":
                "S388",
        },
    )


def test_frozen_physical_provenance_hashes_are_exact():
    for path, expected in PROVENANCE_HASHES.items():
        assert _sha256(path) == expected


def test_frozen_policy_and_ucits_identity_are_exact():
    b3d = json.loads(
        (
            ROOT
            / "contracts/research/"
            "phase_c4b3d_summary_v0.1.json"
        ).read_text()
    )

    c4c0 = json.loads(
        (
            ROOT
            / "contracts/research/"
            "phase_c4c0_summary_v0.1.json"
        ).read_text()
    )

    assert (
        b3d[
            "research_dividend_policy_selection"
        ]
        ==
        "TBILL_CRASH_RESERVE_NET28"
    )

    assert (
        b3d[
            "policy_freeze_authorized"
        ]
        is True
    )

    assert (
        c4c0[
            "recovery_isin"
        ]
        ==
        "IE00BMC38736"
    )

    assert (
        c4c0[
            "live_execution_authorized"
        ]
        is False
    )


def test_schema_is_additive_and_does_not_change_machine_state():
    con = sqlite3.connect(
        ":memory:"
    )

    con.row_factory = sqlite3.Row

    ensure_v04_schema(
        con
    )

    before = [
        row[1]
        for row
        in con.execute(
            "PRAGMA table_info(machine_state)"
        ).fetchall()
    ]

    ensure_recovery_schema(
        con
    )

    after = [
        row[1]
        for row
        in con.execute(
            "PRAGMA table_info(machine_state)"
        ).fetchall()
    ]

    assert after == before

    assert (
        con.execute(
            """
            SELECT COUNT(*)
            FROM recovery_state_v01
            """
        ).fetchone()[0]
        ==
        0
    )


def test_initial_state_is_normal_and_idempotent():
    con = sqlite3.connect(
        ":memory:"
    )

    con.row_factory = sqlite3.Row

    first = initialize_recovery_state(
        con,
        reserve_bucket_eur=12.34,
    )

    second = initialize_recovery_state(
        con,
        reserve_bucket_eur=999.0,
    )

    assert first["phase"] == "NORMAL"
    assert first["revision"] == 1
    assert first["reserve_bucket_eur"] == 12.34

    # Initialization is not allowed to rewrite an existing durable state.
    assert second["reserve_bucket_eur"] == 12.34


def test_normal_to_wait_d40_is_durable_and_idempotent():
    con = _db()

    first = transition_recovery_state(
        con,
        event_key="cycle:dotcom:start",
        to_phase="WAIT_D40",
        reason="FIRST_SOURCE_LADDER_EVENT",
        updates={
            "cycle_id":
                "DOTCOM",

            "old_ath":
                100.0,
        },
        payload={
            "target":
                0.10,
        },
    )

    replay = transition_recovery_state(
        con,
        event_key="cycle:dotcom:start",
        to_phase="WAIT_D40",
        reason="FIRST_SOURCE_LADDER_EVENT",
        updates={
            "cycle_id":
                "DOTCOM",

            "old_ath":
                100.0,
        },
        payload={
            "target":
                0.10,
        },
    )

    assert first.status == "APPLIED"
    assert replay.status == "ALREADY_APPLIED"

    state = validate_recovery_state(
        con
    )

    assert state["phase"] == "WAIT_D40"
    assert state["cycle_id"] == "DOTCOM"
    assert state["current_target"] == 0.0


def test_conflicting_transition_replay_fails_closed():
    con = _db()

    transition_recovery_state(
        con,
        event_key="e1",
        to_phase="WAIT_D40",
        reason="R1",
        updates={
            "cycle_id":
                "C1",

            "old_ath":
                100.0,
        },
    )

    with pytest.raises(
        RecoveryStateError
    ):
        transition_recovery_state(
            con,
            event_key="e1",
            to_phase="WAIT_D40",
            reason="DIFFERENT",
            updates={
                "cycle_id":
                    "C1",

                "old_ath":
                    100.0,
            },
        )


def test_transition_replay_includes_exact_updates():
    con = _db()

    transition_recovery_state(
        con,
        event_key="exact-replay",
        to_phase="WAIT_D40",
        reason="START",
        updates={
            "cycle_id":
                "C1",

            "old_ath":
                100.0,
        },
    )

    with pytest.raises(
        RecoveryStateError
    ):
        transition_recovery_state(
            con,
            event_key="exact-replay",
            to_phase="WAIT_D40",
            reason="START",
            updates={
                "cycle_id":
                    "C1",

                "old_ath":
                    101.0,
            },
        )


def test_wait_d40_to_active_requires_h378_clock():
    con = _db()

    transition_recovery_state(
        con,
        event_key="start",
        to_phase="WAIT_D40",
        reason="START",
        updates={
            "cycle_id":
                "C1",

            "old_ath":
                100.0,
        },
    )

    with pytest.raises(
        RecoveryStateError
    ):
        transition_recovery_state(
            con,
            event_key="bad-entry",
            to_phase="RECOVERY_ACTIVE",
            reason="MATURED",
            updates={
                "current_target":
                    0.10,
            },
        )

    assert (
        load_recovery_state(
            con
        )["phase"]
        ==
        "WAIT_D40"
    )


def test_scale_up_keeps_original_first_entry_and_h378_clock():
    con = _db()

    _activate(
        con,
        target=0.10,
    )

    transition_recovery_state(
        con,
        event_key="C1:scale",
        to_phase="RECOVERY_ACTIVE",
        reason="LATER_D40_SCALE",
        updates={
            "current_target":
                0.30,
        },
    )

    state = validate_recovery_state(
        con
    )

    assert state["current_target"] == 0.30

    assert (
        state[
            "first_actual_entry_session"
        ]
        ==
        "S010"
    )

    assert (
        state[
            "fixed_exit_session"
        ]
        ==
        "S388"
    )


def test_active_cycle_cannot_scale_down_before_h378():
    con = _db()

    _activate(
        con,
        target=0.30,
    )

    with pytest.raises(
        RecoveryStateError
    ):
        transition_recovery_state(
            con,
            event_key="C1:scale-down",
            to_phase="RECOVERY_ACTIVE",
            reason="ILLEGAL_SCALE_DOWN",
            updates={
                "current_target":
                    0.10,
            },
        )


def test_later_scale_cannot_move_first_entry_or_h378_exit():
    con = _db()

    _activate(
        con,
        target=0.30,
    )

    with pytest.raises(
        RecoveryStateError
    ):
        transition_recovery_state(
            con,
            event_key="C1:move-clock",
            to_phase="RECOVERY_ACTIVE",
            reason="ILLEGAL_CLOCK_RESET",
            updates={
                "fixed_exit_session":
                    "S400",
            },
        )


def test_fixed_exit_enters_old_ath_guard_then_rearms():
    con = _db()

    _activate(
        con,
        target=1.0,
    )

    transition_recovery_state(
        con,
        event_key="C1:exit",
        to_phase="OLD_ATH_GUARD",
        reason="H378_EXIT",
        updates={
            "current_target":
                0.0,
        },
    )

    state = validate_recovery_state(
        con
    )

    assert (
        state["phase"]
        ==
        "OLD_ATH_GUARD"
    )

    assert (
        state[
            "first_actual_entry_session"
        ]
        ==
        "S010"
    )

    assert (
        state[
            "fixed_exit_session"
        ]
        ==
        "S388"
    )

    transition_recovery_state(
        con,
        event_key="C1:rearm",
        to_phase="NORMAL",
        reason="OLD_ATH_RECOVERED",
        updates={
            "cycle_id":
                None,

            "old_ath":
                None,

            "current_target":
                0.0,

            "first_actual_entry_session":
                None,

            "fixed_exit_session":
                None,

            "old_ath_recovered":
                0,
        },
    )

    assert (
        validate_recovery_state(
            con
        )["phase"]
        ==
        "NORMAL"
    )


def test_wait_d40_cycle_can_cancel_before_first_entry():
    con = _db()

    transition_recovery_state(
        con,
        event_key="start",
        to_phase="WAIT_D40",
        reason="START",
        updates={
            "cycle_id":
                "C1",

            "old_ath":
                100.0,
        },
    )

    transition_recovery_state(
        con,
        event_key="cancel",
        to_phase="NORMAL",
        reason="OLD_ATH_RECOVERED_BEFORE_FIRST_DELAYED_ENTRY",
        updates={
            "cycle_id":
                None,

            "old_ath":
                None,

            "current_target":
                0.0,

            "first_actual_entry_session":
                None,

            "fixed_exit_session":
                None,

            "old_ath_recovered":
                0,
        },
    )

    assert (
        validate_recovery_state(
            con
        )["phase"]
        ==
        "NORMAL"
    )


def test_delayed_events_are_idempotent_and_maturity_ordered():
    con = _db()

    second = enqueue_delayed_event(
        con,
        source_event_key="src2",
        cycle_id="C1",
        source_execution_session="S020",
        maturity_session="S060",
        target=0.30,
    )

    first = enqueue_delayed_event(
        con,
        source_event_key="src1",
        cycle_id="C1",
        source_execution_session="S010",
        maturity_session="S050",
        target=0.10,
    )

    replay = enqueue_delayed_event(
        con,
        source_event_key="src1",
        cycle_id="C1",
        source_execution_session="S010",
        maturity_session="S050",
        target=0.10,
    )

    assert second["status"] == "PENDING"
    assert first["id"] == replay["id"]

    rows = pending_events(
        con,
        cycle_id="C1",
    )

    assert [
        row[
            "source_event_key"
        ]
        for row
        in rows
    ] == [
        "src1",
        "src2",
    ]


def test_conflicting_delayed_event_replay_fails_closed():
    con = _db()

    enqueue_delayed_event(
        con,
        source_event_key="src",
        cycle_id="C1",
        source_execution_session="S010",
        maturity_session="S050",
        target=0.10,
    )

    with pytest.raises(
        RecoveryStateError
    ):
        enqueue_delayed_event(
            con,
            source_event_key="src",
            cycle_id="C1",
            source_execution_session="S010",
            maturity_session="S051",
            target=0.10,
        )


def test_delayed_event_status_machine_fails_closed():
    con = _db()

    enqueue_delayed_event(
        con,
        source_event_key="src",
        cycle_id="C1",
        source_execution_session="S010",
        maturity_session="S050",
        target=0.10,
    )

    row = set_delayed_event_status(
        con,
        source_event_key="src",
        to_status="MATURED",
    )

    assert row["status"] == "MATURED"

    row = set_delayed_event_status(
        con,
        source_event_key="src",
        to_status="APPLIED",
    )

    assert row["status"] == "APPLIED"

    with pytest.raises(
        RecoveryStateError
    ):
        set_delayed_event_status(
            con,
            source_event_key="src",
            to_status="PENDING",
        )


def test_reserve_bucket_credit_debit_and_replay_are_durable():
    con = _db()

    credit = apply_reserve_ledger_event(
        con,
        event_key="dividend:AAPL:1",
        amount_eur=100.0,
        reason="NET_DIVIDEND",
    )

    debit = apply_reserve_ledger_event(
        con,
        event_key="recovery-funding:1",
        amount_eur=-40.0,
        reason="RECOVERY_FUNDING",
    )

    replay = apply_reserve_ledger_event(
        con,
        event_key="recovery-funding:1",
        amount_eur=-40.0,
        reason="RECOVERY_FUNDING",
    )

    assert credit.new_balance_eur == 100.0
    assert debit.new_balance_eur == 60.0

    assert (
        replay.status
        ==
        "ALREADY_APPLIED"
    )

    assert (
        load_recovery_state(
            con
        )[
            "reserve_bucket_eur"
        ]
        ==
        60.0
    )


def test_reserve_bucket_overdraw_is_forbidden_and_atomic():
    con = _db()

    apply_reserve_ledger_event(
        con,
        event_key="credit",
        amount_eur=10.0,
        reason="NET_DIVIDEND",
    )

    with pytest.raises(
        RecoveryStateError
    ):
        apply_reserve_ledger_event(
            con,
            event_key="too-much",
            amount_eur=-10.01,
            reason="RECOVERY_FUNDING",
        )

    assert (
        load_recovery_state(
            con
        )[
            "reserve_bucket_eur"
        ]
        ==
        10.0
    )

    assert (
        con.execute(
            """
            SELECT COUNT(*)
            FROM recovery_reserve_ledger_v01
            WHERE event_key='too-much'
            """
        ).fetchone()[0]
        ==
        0
    )
