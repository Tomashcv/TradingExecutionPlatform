from __future__ import annotations

import sqlite3

import pytest

from sp1execution.execution.workflow_v04 import (
    LegSpec,
    WorkflowError,
    _set_execution_state,
    create_workflow,
    install_buy_legs,
    mark_buy_reconciliation_result,
    mark_sell_reconciliation_result,
    record_broker_acceptance,
    record_leg_intent,
    require_reconciliation_for_ambiguous_intent,
    signed_broker_quantity,
    start_workflow,
)
from sp1execution.state.v04_store import ensure_schema

NOW = "2026-08-13T20:45:00+00:00"


def _db():
    con = sqlite3.connect(":memory:")
    ensure_schema(con)
    con.execute(
        "INSERT INTO machine_state(id,schema_version,revision,lifecycle_state,entry_policy,entry_state,strategy_state,membership_state,execution_state,active_membership_month,active_membership_json,active_overlay,sp2_mix_json,old_peak,trough,rearm_old_ath,capital_basis_eur,strategy_cash_eur,external_cash_debt_eur,realized_fees_eur,realized_fx_eur,marked_nav_eur,created_at,updated_at) VALUES(1,'0.4.0',1,'DEMO','IMMEDIATE_SP2','ENTRY_COMPLETE','NORMAL','ACTIVE','IDLE','2026-07','{\"month\":\"2026-07\",\"symbols\":[\"AAPL\",\"NVDA\"]}',0.0,'{\"AAPL\":0.5,\"NVDA\":0.5}',NULL,NULL,NULL,10000.0,-50.0,50.0,10.0,0.0,NULL,?,?)",
        (NOW, NOW),
    )
    con.commit()
    return con


def _state(con):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM machine_state WHERE id=1").fetchone()
    con.row_factory = None
    return dict(row)


def _workflow(con):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM execution_workflows WHERE workflow_id='wf1'").fetchone()
    con.row_factory = None
    return dict(row)


def _legs(con):
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM execution_legs WHERE workflow_id='wf1' ORDER BY leg_index"
    ).fetchall()
    con.row_factory = None
    return [dict(row) for row in rows]


def _sell():
    return LegSpec("SELL", "NVDA", "NVDA_US_EQ", 10.0, 1900.0)


def _buy(notional=1800.0):
    return LegSpec("BUY", "VUAA", "VUAAm_EQ", 15.0, notional)


def _create_start(con, sell_legs=None):
    if sell_legs is None:
        sell_legs = [_sell()]
    create_workflow(
        con,
        workflow_id="wf1",
        decision_id="decision-1",
        target_payload={"target_weights": {"AAPL": 0.45, "NVDA": 0.45, "VUAA": 0.10}},
        sell_legs=sell_legs,
        created_at=NOW,
    )
    return start_workflow(con, workflow_id="wf1", created_at=NOW)


def _set_capital(con, *, cash, debt):
    con.execute(
        "UPDATE machine_state SET strategy_cash_eur=?,external_cash_debt_eur=? WHERE id=1",
        (cash, debt),
    )
    con.commit()


def _accept_leg(con, leg_index, broker_order_id):
    leg = _legs(con)[leg_index]
    if leg["status"] == "PLANNED":
        record_leg_intent(
            con,
            workflow_id="wf1",
            leg_index=leg_index,
            created_at=NOW,
        )
        leg = _legs(con)[leg_index]
    if leg["status"] == "INTENT_RECORDED":
        record_broker_acceptance(
            con,
            workflow_id="wf1",
            leg_index=leg_index,
            broker_order_id=broker_order_id,
            response={"id": broker_order_id},
            created_at=NOW,
        )


def _mark_sell(con, status):
    _accept_leg(con, 0, "sell-0")
    return mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: status},
        created_at=NOW,
    )


def _mark_buy(con, buy_index, status):
    _accept_leg(con, buy_index, f"buy-{buy_index}")
    return mark_buy_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={buy_index: status},
        created_at=NOW,
    )


def test_create_is_durable_and_idempotent():
    con = _db()
    kwargs = {
        "workflow_id": "wf1",
        "decision_id": "decision-1",
        "target_payload": {"x": 1},
        "sell_legs": [_sell()],
        "created_at": NOW,
    }
    first = create_workflow(con, **kwargs)
    second = create_workflow(con, **kwargs)
    assert first.execution_state == "PLAN_CREATED"
    assert second.workflow_id == "wf1"
    assert _state(con)["execution_state"] == "PLAN_CREATED"
    assert con.execute("SELECT COUNT(*) FROM execution_workflows").fetchone()[0] == 1


def test_conflicting_workflow_replay_fails_closed():
    con = _db()
    create_workflow(
        con,
        workflow_id="wf1",
        decision_id="d1",
        target_payload={"x": 1},
        sell_legs=[_sell()],
        created_at=NOW,
    )
    with pytest.raises(WorkflowError, match="conflicting workflow replay"):
        create_workflow(
            con,
            workflow_id="wf1",
            decision_id="d1",
            target_payload={"x": 2},
            sell_legs=[_sell()],
            created_at=NOW,
        )


def test_mixed_path_starts_sell_first():
    con = _db()
    result = _create_start(con)
    assert result.phase == "SELL"
    assert result.execution_state == "SELL_PENDING"


def test_no_sell_path_waits_in_reconcile_before_buy():
    con = _db()
    result = _create_start(con, sell_legs=[])
    assert result.phase == "RECONCILE"
    assert result.execution_state == "RECONCILING"


def test_intent_precedes_broker_acceptance():
    con = _db()
    _create_start(con)
    record_leg_intent(con, workflow_id="wf1", leg_index=0, created_at=NOW)
    assert _legs(con)[0]["status"] == "INTENT_RECORDED"
    record_broker_acceptance(
        con,
        workflow_id="wf1",
        leg_index=0,
        broker_order_id="o1",
        response={"id": "o1"},
        created_at=NOW,
    )
    leg = _legs(con)[0]
    assert leg["status"] == "BROKER_ACCEPTED"
    assert leg["broker_order_id"] == "o1"


def test_ambiguous_post_window_fails_closed():
    con = _db()
    _create_start(con)
    record_leg_intent(con, workflow_id="wf1", leg_index=0, created_at=NOW)
    assert require_reconciliation_for_ambiguous_intent(con, workflow_id="wf1", created_at=NOW)
    assert _state(con)["execution_state"] == "RECONCILIATION_REQUIRED"
    assert _workflow(con)["status"] == "RECONCILIATION_REQUIRED"


def test_partial_sell_blocks_buy():
    con = _db()
    _create_start(con)
    result = _mark_sell(con, "PARTIAL")
    assert result.execution_state == "PARTIAL_FILL"
    with pytest.raises(WorkflowError, match="ACTIVE RECONCILE"):
        install_buy_legs(
            con,
            workflow_id="wf1",
            buy_legs=[_buy()],
            created_at=NOW,
        )


def test_unknown_sell_fails_closed():
    con = _db()
    _create_start(con)
    result = _mark_sell(con, "UNKNOWN")
    assert result.status == "RECONCILIATION_REQUIRED"
    assert result.execution_state == "RECONCILIATION_REQUIRED"


def test_all_sells_filled_enters_reconcile_not_buy_directly():
    con = _db()
    _create_start(con)
    result = _mark_sell(con, "FILLED")
    assert result.phase == "RECONCILE"
    assert result.execution_state == "RECONCILING"


def test_buy_requires_debt_repaid():
    con = _db()
    _create_start(con)
    _mark_sell(con, "FILLED")
    with pytest.raises(WorkflowError, match="external cash debt remains"):
        install_buy_legs(
            con,
            workflow_id="wf1",
            buy_legs=[_buy()],
            created_at=NOW,
        )


def test_buy_notional_cannot_exceed_realized_strategy_cash():
    con = _db()
    _set_capital(con, cash=900.0, debt=0.0)
    _create_start(con)
    _mark_sell(con, "FILLED")
    with pytest.raises(WorkflowError, match="exceeds realized strategy cash"):
        install_buy_legs(
            con,
            workflow_id="wf1",
            buy_legs=[_buy(1000.0)],
            created_at=NOW,
        )


def test_buy_install_then_full_fill_completes_idle():
    con = _db()
    _set_capital(con, cash=2000.0, debt=0.0)
    _create_start(con)
    _mark_sell(con, "FILLED")
    installed = install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )
    assert installed.execution_state == "BUY_PENDING"
    buy_index = _legs(con)[1]["leg_index"]
    final = _mark_buy(con, buy_index, "FILLED")
    assert final.status == "COMPLETE"
    assert final.execution_state == "IDLE"
    assert _workflow(con)["status"] == "COMPLETE"


def test_partial_buy_does_not_return_idle():
    con = _db()
    _set_capital(con, cash=2000.0, debt=0.0)
    _create_start(con)
    _mark_sell(con, "FILLED")
    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )
    buy_index = _legs(con)[1]["leg_index"]
    result = _mark_buy(con, buy_index, "PARTIAL")
    assert result.execution_state == "PARTIAL_FILL"
    assert _workflow(con)["status"] == "ACTIVE"


def test_signed_quantity_contract():
    assert signed_broker_quantity("SELL", 3.25) == -3.25
    assert signed_broker_quantity("BUY", 3.25) == 3.25


def test_reconciliation_cannot_promote_unsubmitted_sell():
    con = _db()
    _create_start(con)

    with pytest.raises(
        WorkflowError,
        match="durable broker_order_id",
    ):
        mark_sell_reconciliation_result(
            con,
            workflow_id="wf1",
            leg_statuses={0: "FILLED"},
            created_at=NOW,
        )


def test_buy_leg_requires_notional_evidence():
    con = _db()
    _set_capital(con, cash=2000.0, debt=0.0)
    _create_start(con)
    _mark_sell(con, "FILLED")

    with pytest.raises(
        WorkflowError,
        match="requires estimated_notional_eur",
    ):
        install_buy_legs(
            con,
            workflow_id="wf1",
            buy_legs=[_buy(None)],
            created_at=NOW,
        )


def test_transition_replay_conflict_detected_even_at_same_state():
    con = _db()
    create_workflow(
        con,
        workflow_id="wf1",
        decision_id="decision-1",
        target_payload={"x": 1},
        sell_legs=[_sell()],
        created_at=NOW,
    )

    with pytest.raises(
        WorkflowError,
        match="conflicting execution transition replay",
    ), con:
        _set_execution_state(
            con,
            workflow_id="wf1",
            event_suffix="created",
            to_state="PLAN_CREATED",
            reason="conflicting_reason",
            created_at=NOW,
        )


def test_ambiguous_intent_requires_real_active_workflow():
    con = _db()
    with pytest.raises(WorkflowError, match="unknown workflow"):
        require_reconciliation_for_ambiguous_intent(
            con,
            workflow_id="missing",
            created_at=NOW,
        )
