from __future__ import annotations

import sqlite3

import pytest

from sp1execution.execution.recovery_v04 import (
    RecoveryInvariantError,
    classify_recovery,
    discover_recoverable_workflow,
)
from sp1execution.execution.workflow_v04 import (
    LegSpec,
    create_workflow,
    install_buy_legs,
    mark_buy_reconciliation_result,
    mark_sell_reconciliation_result,
    record_broker_acceptance,
    record_leg_intent,
    start_workflow,
)
from sp1execution.state.capital_v04 import (
    NormalizedFillEvent,
    apply_fill_event,
)
from sp1execution.state.v04_store import ensure_schema

NOW = "2026-08-13T21:50:00+00:00"


def _db(
    *,
    cash=2000.0,
    debt=0.0,
):
    con = sqlite3.connect(":memory:")
    ensure_schema(con)

    con.execute(
        """
        INSERT INTO machine_state(
            id,
            schema_version,
            revision,
            lifecycle_state,
            entry_policy,
            entry_state,
            strategy_state,
            membership_state,
            execution_state,
            active_membership_month,
            active_membership_json,
            active_overlay,
            sp2_mix_json,
            old_peak,
            trough,
            rearm_old_ath,
            capital_basis_eur,
            strategy_cash_eur,
            external_cash_debt_eur,
            realized_fees_eur,
            realized_fx_eur,
            marked_nav_eur,
            created_at,
            updated_at
        )
        VALUES(
            1,
            '0.4.0',
            1,
            'DEMO',
            'IMMEDIATE_SP2',
            'ENTRY_COMPLETE',
            'NORMAL',
            'ACTIVE',
            'IDLE',
            '2026-07',
            '{"month":"2026-07","symbols":["AAPL","NVDA"]}',
            0.0,
            '{"AAPL":0.5,"NVDA":0.5}',
            NULL,
            NULL,
            NULL,
            10000.0,
            ?,
            ?,
            10.0,
            0.0,
            NULL,
            ?,
            ?
        )
        """,
        (
            cash,
            debt,
            NOW,
            NOW,
        ),
    )
    con.commit()
    return con


def _sell(
    symbol="NVDA",
    ticker="NVDA_US_EQ",
    quantity=5.0,
):
    return LegSpec(
        side="SELL",
        logical_symbol=symbol,
        broker_ticker=ticker,
        quantity=quantity,
        estimated_notional_eur=1000.0,
    )


def _buy(
    symbol="VUAA",
    ticker="VUAAm_EQ",
    quantity=5.0,
    notional=500.0,
):
    return LegSpec(
        side="BUY",
        logical_symbol=symbol,
        broker_ticker=ticker,
        quantity=quantity,
        estimated_notional_eur=notional,
    )


def _create(
    con,
    *,
    workflow_id="wf1",
    sell_legs=None,
):
    if sell_legs is None:
        sell_legs = [_sell()]

    return create_workflow(
        con,
        workflow_id=workflow_id,
        decision_id=f"decision:{workflow_id}",
        target_payload={"target_weights": {"AAPL": 0.5}},
        sell_legs=sell_legs,
        created_at=NOW,
    )


def _start(
    con,
    *,
    workflow_id="wf1",
):
    return start_workflow(
        con,
        workflow_id=workflow_id,
        created_at=NOW,
    )


def _accept(
    con,
    *,
    workflow_id="wf1",
    leg_index=0,
    broker_order_id="order-1",
):
    record_leg_intent(
        con,
        workflow_id=workflow_id,
        leg_index=leg_index,
        created_at=NOW,
    )
    record_broker_acceptance(
        con,
        workflow_id=workflow_id,
        leg_index=leg_index,
        broker_order_id=broker_order_id,
        response={"id": broker_order_id},
        created_at=NOW,
    )


def _decision(
    con,
    *,
    workflow_id="wf1",
    persist_ambiguous=True,
):
    return classify_recovery(
        con,
        workflow_id=workflow_id,
        created_at=NOW,
        persist_ambiguous=persist_ambiguous,
    )


def _state(con):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM machine_state WHERE id=1").fetchone()
    con.row_factory = None
    return dict(row)


def _workflow(con, workflow_id="wf1"):
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        SELECT *
        FROM execution_workflows
        WHERE workflow_id=?
        """,
        (workflow_id,),
    ).fetchone()
    con.row_factory = None
    return dict(row)


def _fill_event(
    *,
    order_id,
    fill_id,
    side,
    cash_delta,
):
    wallet = abs(cash_delta)

    return NormalizedFillEvent(
        event_key=f"t212:fill:{order_id}:{fill_id}",
        broker_order_id=order_id,
        fill_id=fill_id,
        ticker=("NVDA_US_EQ" if side == "SELL" else "VUAAm_EQ"),
        side=side,
        filled_at=NOW,
        quantity=5.0,
        price=100.0,
        cash_delta_eur=cash_delta,
        fee_eur=1.0,
        fx_rate=1.15,
        wallet_net_value_eur=wallet,
        payload=('{"schema":"m6_fault_injection_test"}'),
    )


def test_idle_without_workflow_is_no_workflow():
    con = _db()

    result = classify_recovery(
        con,
        created_at=NOW,
    )

    assert result.action == "NO_WORKFLOW"
    assert not result.may_submit_order
    assert not result.requires_broker_read


def test_crash_after_workflow_create_before_start_resumes_start():
    con = _db()
    _create(con)

    result = _decision(con)

    assert result.action == "START_WORKFLOW"
    assert not result.may_submit_order


def test_crash_after_start_before_sell_post_may_submit_sell():
    con = _db()
    _create(con)
    _start(con)

    result = _decision(con)

    assert result.action == "SUBMIT_SELL"
    assert result.may_submit_order


def test_crash_after_sell_intent_is_fail_closed_no_retry():
    con = _db()
    _create(con)
    _start(con)

    record_leg_intent(
        con,
        workflow_id="wf1",
        leg_index=0,
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "MANUAL_RECONCILIATION"
    assert not result.may_submit_order
    assert _state(con)["execution_state"] == "RECONCILIATION_REQUIRED"
    assert _workflow(con)["status"] == "RECONCILIATION_REQUIRED"


def test_post_return_before_order_id_persist_is_same_ambiguous_window():
    con = _db()
    _create(con)
    _start(con)

    record_leg_intent(
        con,
        workflow_id="wf1",
        leg_index=0,
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "MANUAL_RECONCILIATION"
    assert not result.may_submit_order


def test_durable_sell_broker_id_requires_reconciliation_not_resubmit():
    con = _db()
    _create(con)
    _start(con)
    _accept(con)

    result = _decision(con)

    assert result.action == "RECONCILE_SELL"
    assert not result.may_submit_order
    assert result.requires_broker_read


def test_timeout_does_not_convert_known_sell_order_into_resubmit():
    con = _db()
    _create(con)
    _start(con)
    _accept(con)

    con.execute(
        """
        UPDATE execution_legs
        SET updated_at='2025-01-01T00:00:00+00:00'
        WHERE workflow_id='wf1'
        """
    )
    con.commit()

    result = _decision(con)

    assert result.action == "RECONCILE_SELL"
    assert not result.may_submit_order


def test_partial_sell_restart_reconciles_and_never_buys():
    con = _db()
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "PARTIAL"},
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "RECONCILE_SELL"
    assert not result.may_submit_order


def test_sell_fill_ledger_applied_before_leg_transition_still_reconciles():
    con = _db(cash=-50.0, debt=50.0)
    _create(con)
    _start(con)
    _accept(con)

    apply_fill_event(
        con,
        _fill_event(
            order_id="order-1",
            fill_id="sell-fill-1",
            side="SELL",
            cash_delta=1000.0,
        ),
        decision_id="decision:wf1",
    )

    assert _state(con)["strategy_cash_eur"] == 950.0
    assert _state(con)["external_cash_debt_eur"] == 0.0

    result = _decision(con)

    assert result.action == "RECONCILE_SELL"
    assert not result.may_submit_order


def test_after_sell_reconciliation_restart_replans_buys():
    con = _db()
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "REPLAN_BUYS"
    assert not result.may_submit_order


def test_after_buy_plan_before_post_may_submit_buy():
    con = _db(cash=2000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "SUBMIT_BUY"
    assert result.may_submit_order


def test_buy_intent_without_order_id_is_never_retried():
    con = _db(cash=2000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )

    record_leg_intent(
        con,
        workflow_id="wf1",
        leg_index=1,
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "MANUAL_RECONCILIATION"
    assert not result.may_submit_order


def test_durable_buy_broker_id_requires_reconciliation():
    con = _db(cash=2000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )

    _accept(
        con,
        leg_index=1,
        broker_order_id="buy-1",
    )

    result = _decision(con)

    assert result.action == "RECONCILE_BUY"
    assert not result.may_submit_order
    assert result.requires_broker_read


def test_partial_buy_restart_reconciles_never_submits_next():
    con = _db(cash=2000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )
    _accept(
        con,
        leg_index=1,
        broker_order_id="buy-1",
    )

    mark_buy_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={1: "PARTIAL"},
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "RECONCILE_BUY"
    assert not result.may_submit_order


def test_buy_fill_ledger_applied_before_final_state_still_reconciles():
    con = _db(cash=1000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy(notional=500.0)],
        created_at=NOW,
    )
    _accept(
        con,
        leg_index=1,
        broker_order_id="buy-1",
    )

    apply_fill_event(
        con,
        _fill_event(
            order_id="buy-1",
            fill_id="buy-fill-1",
            side="BUY",
            cash_delta=-500.0,
        ),
        decision_id="decision:wf1",
    )

    assert _state(con)["strategy_cash_eur"] == 500.0

    result = _decision(con)

    assert result.action == "RECONCILE_BUY"
    assert not result.may_submit_order


def test_complete_workflow_is_terminal_no_submission():
    con = _db(cash=2000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )
    _accept(
        con,
        leg_index=1,
        broker_order_id="buy-1",
    )

    mark_buy_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={1: "FILLED"},
        created_at=NOW,
    )

    result = _decision(con)

    assert result.action == "COMPLETE"
    assert not result.may_submit_order


def test_reconciliation_required_remains_manual_after_restart():
    con = _db()
    _create(con)
    _start(con)

    record_leg_intent(
        con,
        workflow_id="wf1",
        leg_index=0,
        created_at=NOW,
    )

    first = _decision(con)
    second = _decision(con)

    assert first.action == "MANUAL_RECONCILIATION"
    assert second.action == "MANUAL_RECONCILIATION"
    assert not second.may_submit_order


def test_multiple_noncomplete_workflows_fail_closed():
    con = _db()
    _create(
        con,
        workflow_id="wf1",
    )

    # Deliberately inject a second recoverable workflow to
    # test the recovery invariant. This bypasses the M5A
    # single-active-workflow guard on purpose.
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
            'wf2',
            'decision:wf2',
            'TWO_PHASE_REBALANCE',
            'FAILED',
            'SELL',
            1,
            '{}',
            ?,
            ?
        )
        """,
        (
            NOW,
            NOW,
        ),
    )
    con.commit()

    with pytest.raises(
        RecoveryInvariantError,
        match="multiple non-complete",
    ):
        discover_recoverable_workflow(con)


def test_nonidle_without_workflow_fails_closed():
    con = _db()

    con.execute(
        """
        UPDATE machine_state
        SET execution_state='FAILED'
        WHERE id=1
        """
    )
    con.commit()

    with pytest.raises(
        RecoveryInvariantError,
        match="no recoverable workflow",
    ):
        classify_recovery(
            con,
            created_at=NOW,
        )


def test_planned_buy_with_external_debt_fails_closed():
    con = _db(cash=1000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[_buy()],
        created_at=NOW,
    )

    con.execute(
        """
        UPDATE machine_state
        SET
            strategy_cash_eur=-10.0,
            external_cash_debt_eur=10.0
        WHERE id=1
        """
    )
    con.commit()

    with pytest.raises(
        RecoveryInvariantError,
        match="external cash debt remains",
    ):
        _decision(con)


def test_explicit_workflow_id_cannot_bypass_competing_workflow_guard():
    con = _db()
    _create(con, workflow_id="wf1")

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
            'wf2',
            'decision:wf2',
            'TWO_PHASE_REBALANCE',
            'FAILED',
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
        RecoveryInvariantError,
        match="multiple non-complete",
    ):
        classify_recovery(
            con,
            workflow_id="wf1",
            created_at=NOW,
        )


def test_mid_batch_sell_restart_submits_only_remaining_planned_work():
    con = _db()

    _create(
        con,
        sell_legs=[
            _sell(
                symbol="NVDA",
                ticker="NVDA_US_EQ",
                quantity=5.0,
            ),
            _sell(
                symbol="AAPL",
                ticker="AAPL_US_EQ",
                quantity=3.0,
            ),
        ],
    )
    _start(con)

    _accept(
        con,
        leg_index=0,
        broker_order_id="sell-0",
    )

    result = _decision(con)

    assert result.action == "SUBMIT_SELL"
    assert result.may_submit_order

    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT leg_index,status,broker_order_id
        FROM execution_legs
        WHERE workflow_id='wf1'
        ORDER BY leg_index
        """
    ).fetchall()
    con.row_factory = None

    assert dict(rows[0]) == {
        "leg_index": 0,
        "status": "BROKER_ACCEPTED",
        "broker_order_id": "sell-0",
    }
    assert dict(rows[1]) == {
        "leg_index": 1,
        "status": "PLANNED",
        "broker_order_id": None,
    }


def test_sequential_buy_restart_after_first_fill_allows_next_planned_buy():
    con = _db(cash=2000.0, debt=0.0)
    _create(con)
    _start(con)
    _accept(con)

    mark_sell_reconciliation_result(
        con,
        workflow_id="wf1",
        leg_statuses={0: "FILLED"},
        created_at=NOW,
    )

    install_buy_legs(
        con,
        workflow_id="wf1",
        buy_legs=[
            _buy(
                symbol="VUAA",
                ticker="VUAAm_EQ",
                quantity=5.0,
                notional=500.0,
            ),
            _buy(
                symbol="AAPL",
                ticker="AAPL_US_EQ",
                quantity=1.0,
                notional=250.0,
            ),
        ],
        created_at=NOW,
    )

    _accept(
        con,
        leg_index=1,
        broker_order_id="buy-1",
    )

    con.execute(
        """
        UPDATE execution_legs
        SET status='FILLED'
        WHERE workflow_id='wf1' AND leg_index=1
        """
    )
    con.commit()

    result = _decision(con)

    assert result.action == "SUBMIT_BUY"
    assert result.may_submit_order
