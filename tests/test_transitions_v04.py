from __future__ import annotations

import json
import sqlite3

import pytest

from sp1execution.state.transitions_v04 import (
    StateTransitionError,
    apply_entry_transition,
    apply_strategy_transition,
    begin_month_end,
    classify_membership_candidate,
    classify_month_end,
    commit_membership_rebalance,
)
from sp1execution.state.v04_store import ensure_schema

NOW = "2026-08-13T19:30:00+00:00"


def _db(
    *,
    entry_state="ENTRY_COMPLETE",
    entry_policy="IMMEDIATE_SP2",
    strategy_state="NORMAL",
    membership_state="ACTIVE",
    execution_state="IDLE",
    membership=None,
    mix=None,
    overlay=0.0,
):
    if membership is None:
        membership = {
            "month": "2026-07",
            "symbols": ["AAPL", "NVDA"],
        }

    if mix is None:
        mix = {
            "AAPL": 0.5,
            "NVDA": 0.5,
        }

    con = sqlite3.connect(":memory:")
    ensure_schema(con)

    row = {
        "id": 1,
        "schema_version": "0.4.0",
        "revision": 1,
        "lifecycle_state": "DEMO",
        "entry_policy": entry_policy,
        "entry_state": entry_state,
        "strategy_state": strategy_state,
        "membership_state": membership_state,
        "execution_state": execution_state,
        "active_membership_month": (membership["month"] if membership else None),
        "active_membership_json": (
            json.dumps(
                membership,
                sort_keys=True,
                separators=(",", ":"),
            )
            if membership
            else None
        ),
        "active_overlay": overlay,
        "sp2_mix_json": (
            json.dumps(
                mix,
                sort_keys=True,
                separators=(",", ":"),
            )
            if mix
            else None
        ),
        "old_peak": None,
        "trough": None,
        "rearm_old_ath": None,
        "capital_basis_eur": 10000.0,
        "strategy_cash_eur": 0.0,
        "external_cash_debt_eur": 0.0,
        "realized_fees_eur": 0.0,
        "realized_fx_eur": 0.0,
        "marked_nav_eur": None,
        "created_at": NOW,
        "updated_at": NOW,
    }

    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)

    con.execute(
        f"INSERT INTO machine_state({columns}) VALUES({placeholders})",
        tuple(row.values()),
    )

    con.commit()
    return con


def _state(con):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM machine_state WHERE id=1").fetchone()
    con.row_factory = None
    return dict(row)


def test_strategy_normal_to_crash_is_durable():
    con = _db()

    result = apply_strategy_transition(
        con,
        event_key="robust:2026-01-01:-30",
        to_state="CRASH",
        reason="robust_threshold_reached",
        active_overlay=0.10,
        old_peak=100.0,
        trough=70.0,
        rearm_old_ath=None,
        created_at=NOW,
    )

    state = _state(con)

    assert result.status == "APPLIED"
    assert result.from_state == "NORMAL"
    assert result.to_state == "CRASH"
    assert state["strategy_state"] == "CRASH"
    assert state["active_overlay"] == 0.10
    assert state["old_peak"] == 100.0
    assert state["trough"] == 70.0
    assert state["revision"] == 2


def test_strategy_crash_threshold_update_same_state_allowed():
    con = _db(strategy_state="CRASH", overlay=0.10)

    con.execute(
        """
        UPDATE machine_state
        SET old_peak=100.0, trough=70.0
        WHERE id=1
        """
    )
    con.commit()

    result = apply_strategy_transition(
        con,
        event_key="robust:2026-01-02:-35",
        to_state="CRASH",
        reason="robust_deeper_threshold_reached",
        active_overlay=0.30,
        old_peak=100.0,
        trough=65.0,
        rearm_old_ath=None,
        created_at=NOW,
    )

    state = _state(con)

    assert result.status == "APPLIED"
    assert state["strategy_state"] == "CRASH"
    assert state["active_overlay"] == 0.30
    assert state["trough"] == 65.0


def test_strategy_crash_to_post_handoff():
    con = _db(strategy_state="CRASH", overlay=0.60)

    con.execute(
        """
        UPDATE machine_state
        SET old_peak=100.0, trough=55.0
        WHERE id=1
        """
    )
    con.commit()

    apply_strategy_transition(
        con,
        event_key="robust:2026-02-01:handoff55",
        to_state="POST_HANDOFF",
        reason="robust_recovery_55_handoff",
        active_overlay=0.0,
        old_peak=100.0,
        trough=55.0,
        rearm_old_ath=100.0,
        created_at=NOW,
    )

    state = _state(con)

    assert state["strategy_state"] == "POST_HANDOFF"
    assert state["active_overlay"] == 0.0
    assert state["rearm_old_ath"] == 100.0


def test_post_handoff_to_normal_clears_crash_fields():
    con = _db(strategy_state="POST_HANDOFF")

    con.execute(
        """
        UPDATE machine_state
        SET
            active_overlay=0.0,
            old_peak=100.0,
            trough=55.0,
            rearm_old_ath=100.0
        WHERE id=1
        """
    )
    con.commit()

    apply_strategy_transition(
        con,
        event_key="robust:2026-03-01:rearm",
        to_state="NORMAL",
        reason="old_ath_recovered_rearm",
        active_overlay=0.0,
        old_peak=101.0,
        trough=None,
        rearm_old_ath=None,
        created_at=NOW,
    )

    state = _state(con)

    assert state["strategy_state"] == "NORMAL"
    assert state["active_overlay"] == 0.0
    assert state["trough"] is None
    assert state["rearm_old_ath"] is None


def test_illegal_strategy_jump_fails_closed():
    con = _db()

    with pytest.raises(
        StateTransitionError,
        match="illegal transition",
    ):
        apply_strategy_transition(
            con,
            event_key="illegal",
            to_state="POST_HANDOFF",
            reason="illegal",
            active_overlay=0.0,
            old_peak=100.0,
            trough=50.0,
            rearm_old_ath=100.0,
            created_at=NOW,
        )


def test_transition_replay_is_idempotent():
    con = _db()

    kwargs = {
        "event_key": "robust:2026-01-01:-30",
        "to_state": "CRASH",
        "reason": "robust_threshold_reached",
        "active_overlay": 0.10,
        "old_peak": 100.0,
        "trough": 70.0,
        "rearm_old_ath": None,
        "created_at": NOW,
    }

    first = apply_strategy_transition(con, **kwargs)
    second = apply_strategy_transition(con, **kwargs)

    assert first.status == "APPLIED"
    assert second.status == "ALREADY_APPLIED"
    assert _state(con)["revision"] == 2

    assert (
        con.execute(
            """
            SELECT COUNT(*)
            FROM state_transitions
            WHERE event_key=?
            """,
            (kwargs["event_key"],),
        ).fetchone()[0]
        == 1
    )


def test_conflicting_event_replay_fails_closed():
    con = _db()

    apply_strategy_transition(
        con,
        event_key="same-key",
        to_state="CRASH",
        reason="a",
        active_overlay=0.10,
        old_peak=100.0,
        trough=70.0,
        rearm_old_ath=None,
        created_at=NOW,
    )

    with pytest.raises(
        StateTransitionError,
        match="conflicting replay",
    ):
        apply_strategy_transition(
            con,
            event_key="same-key",
            to_state="CRASH",
            reason="different",
            active_overlay=0.30,
            old_peak=100.0,
            trough=65.0,
            rearm_old_ath=None,
            created_at=NOW,
        )


def test_control_transition_blocked_when_execution_not_idle():
    con = _db(execution_state="SELL_PENDING")

    with pytest.raises(
        StateTransitionError,
        match="execution_state=IDLE",
    ):
        apply_strategy_transition(
            con,
            event_key="blocked",
            to_state="CRASH",
            reason="blocked",
            active_overlay=0.10,
            old_peak=100.0,
            trough=70.0,
            rearm_old_ath=None,
            created_at=NOW,
        )


def test_control_transition_blocked_by_active_workflow():
    con = _db()

    con.execute(
        """
        INSERT INTO execution_workflows(
            workflow_id,
            decision_id,
            kind,
            status,
            phase,
            source_state_revision,
            target_payload,
            created_at,
            updated_at
        )
        VALUES(
            'wf1',
            'd1',
            'TEST',
            'ACTIVE',
            'SELL',
            1,
            '{}',
            ?,
            ?
        )
        """,
        (NOW, NOW),
    )
    con.commit()

    with pytest.raises(
        StateTransitionError,
        match="active execution workflow",
    ):
        apply_strategy_transition(
            con,
            event_key="blocked",
            to_state="CRASH",
            reason="blocked",
            active_overlay=0.10,
            old_peak=100.0,
            trough=70.0,
            rearm_old_ath=None,
            created_at=NOW,
        )


def test_rank_swap_same_set_is_no_trade():
    active = {
        "month": "2026-07",
        "symbols": ["AAPL", "NVDA"],
    }
    candidate = {
        "month": "2026-08",
        "symbols": ["NVDA", "AAPL"],
    }

    assert (
        classify_membership_candidate(
            active_membership=active,
            candidate_membership=candidate,
        )
        == "SAME_SET_NO_TRADE"
    )


def test_month_end_same_set_advances_month_without_rebalance():
    con = _db()

    candidate = {
        "month": "2026-08",
        "symbols": ["NVDA", "AAPL"],
    }

    begin_month_end(
        con,
        event_key="membership:2026-08:begin",
        candidate_membership=candidate,
        created_at=NOW,
    )

    result = classify_month_end(
        con,
        event_key="membership:2026-08:classify",
        candidate_membership=candidate,
        created_at=NOW,
    )

    state = _state(con)

    assert result.to_state == "ACTIVE"
    assert state["membership_state"] == "ACTIVE"
    assert state["active_membership_month"] == "2026-08"

    active = json.loads(state["active_membership_json"])
    assert set(active["symbols"]) == {"AAPL", "NVDA"}

    mix = json.loads(state["sp2_mix_json"])
    assert mix == {"AAPL": 0.5, "NVDA": 0.5}


def test_month_end_set_change_becomes_rebalance_pending():
    con = _db()

    candidate = {
        "month": "2026-08",
        "symbols": ["AAPL", "MSFT"],
    }

    begin_month_end(
        con,
        event_key="membership:2026-08:begin",
        candidate_membership=candidate,
        created_at=NOW,
    )

    result = classify_month_end(
        con,
        event_key="membership:2026-08:classify",
        candidate_membership=candidate,
        created_at=NOW,
    )

    state = _state(con)

    assert result.to_state == "REBALANCE_PENDING"
    assert state["membership_state"] == "REBALANCE_PENDING"
    assert state["active_membership_month"] == "2026-07"


def test_commit_membership_rebalance_sets_new_50_50_mix():
    con = _db(membership_state="REBALANCE_PENDING")

    new_membership = {
        "month": "2026-08",
        "symbols": ["AAPL", "MSFT"],
    }

    result = commit_membership_rebalance(
        con,
        event_key="membership:2026-08:commit",
        new_membership=new_membership,
        created_at=NOW,
        decision_id="decision-123",
    )

    state = _state(con)

    assert result.to_state == "ACTIVE"
    assert state["active_membership_month"] == "2026-08"

    assert json.loads(state["sp2_mix_json"]) == {
        "AAPL": 0.5,
        "MSFT": 0.5,
    }


def test_wait_cash_entry_path_is_one_way():
    con = _db(
        entry_state="UNINITIALIZED",
        entry_policy="UNSET",
        strategy_state="INACTIVE",
        membership_state="ACTIVE",
    )

    first = apply_entry_transition(
        con,
        event_key="entry:wait",
        to_state="WAIT_CASH",
        reason="user_selected_wait_cash",
        created_at=NOW,
    )

    assert first.to_state == "WAIT_CASH"
    assert _state(con)["entry_policy"] == "WAIT_CASH"

    apply_entry_transition(
        con,
        event_key="entry:crash-buy",
        to_state="CRASH_BUY",
        reason="wait_cash_crash_triggered",
        created_at=NOW,
    )

    apply_entry_transition(
        con,
        event_key="entry:handoff",
        to_state="HANDOFF_TO_SP2",
        reason="wait_cash_recovery_handoff",
        created_at=NOW,
    )

    apply_entry_transition(
        con,
        event_key="entry:complete",
        to_state="ENTRY_COMPLETE",
        reason="sp2_handoff_fully_reconciled",
        created_at=NOW,
    )

    assert _state(con)["entry_state"] == "ENTRY_COMPLETE"


def test_immediate_entry_can_complete_directly_after_fill():
    con = _db(
        entry_state="UNINITIALIZED",
        entry_policy="UNSET",
        strategy_state="INACTIVE",
        membership_state="ACTIVE",
    )

    result = apply_entry_transition(
        con,
        event_key="entry:immediate-complete",
        to_state="ENTRY_COMPLETE",
        reason="immediate_sp2_fully_reconciled",
        created_at=NOW,
    )

    state = _state(con)

    assert result.to_state == "ENTRY_COMPLETE"
    assert state["entry_policy"] == "IMMEDIATE_SP2"


def test_non_complete_entry_cannot_run_active_strategy():
    con = _db(
        entry_state="WAIT_CASH",
        entry_policy="WAIT_CASH",
        strategy_state="INACTIVE",
    )

    with pytest.raises(
        StateTransitionError,
        match="non-complete entry",
    ):
        apply_strategy_transition(
            con,
            event_key="entry-not-complete",
            to_state="NORMAL",
            reason="not_allowed",
            active_overlay=0.0,
            old_peak=100.0,
            trough=None,
            rearm_old_ath=None,
            created_at=NOW,
        )


def test_entry_complete_atomically_activates_normal_strategy():
    con = _db(
        entry_state="UNINITIALIZED",
        entry_policy="UNSET",
        strategy_state="INACTIVE",
        membership_state="ACTIVE",
    )

    before = _state(con)
    assert before["revision"] == 1
    assert before["strategy_state"] == "INACTIVE"

    result = apply_entry_transition(
        con,
        event_key="entry:atomic-normal-activation",
        to_state="ENTRY_COMPLETE",
        reason="initial_allocation_fully_reconciled",
        created_at=NOW,
    )

    after = _state(con)

    assert result.revision_before == 1
    assert result.revision_after == 2
    assert after["revision"] == 2
    assert after["entry_state"] == "ENTRY_COMPLETE"
    assert after["strategy_state"] == "NORMAL"

    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        SELECT *
        FROM state_transitions
        WHERE event_key='entry:atomic-normal-activation'
        """
    ).fetchone()
    con.row_factory = None

    payload = json.loads(row["payload"])

    assert payload["coupled_strategy_activation"] == {
        "from": "INACTIVE",
        "reason": "entry_completion_requires_active_strategy",
        "to": "NORMAL",
    }
