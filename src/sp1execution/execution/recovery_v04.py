from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from sp1execution.execution.workflow_v04 import (
    require_reconciliation_for_ambiguous_intent,
)

SUBMIT_ACTIONS = {
    "SUBMIT_SELL",
    "SUBMIT_BUY",
}

BROKER_READ_ACTIONS = {
    "RECONCILE_SELL",
    "RECONCILE_BUY",
}


class RecoveryError(RuntimeError):
    pass


class RecoveryInvariantError(RecoveryError):
    pass


@dataclass(frozen=True)
class RecoveryDecision:
    workflow_id: str | None
    action: str
    reason: str
    workflow_status: str | None
    workflow_phase: str | None
    execution_state: str
    may_submit_order: bool
    requires_broker_read: bool


def _rows(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in con.execute(
                sql,
                params,
            ).fetchall()
        ]
    finally:
        con.row_factory = previous


def _row(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    rows = _rows(
        con,
        sql,
        params,
    )
    return None if not rows else rows[0]


def _machine_state(
    con: sqlite3.Connection,
) -> dict[str, Any]:
    row = _row(
        con,
        "SELECT * FROM machine_state WHERE id=1",
    )
    if row is None:
        raise RecoveryInvariantError("machine_state singleton missing")
    return row


def _workflow(
    con: sqlite3.Connection,
    workflow_id: str,
) -> dict[str, Any]:
    row = _row(
        con,
        """
        SELECT *
        FROM execution_workflows
        WHERE workflow_id=?
        """,
        (workflow_id,),
    )
    if row is None:
        raise RecoveryInvariantError(f"unknown workflow: {workflow_id}")
    return row


def _legs(
    con: sqlite3.Connection,
    workflow_id: str,
    side: str | None = None,
) -> list[dict[str, Any]]:
    if side is None:
        return _rows(
            con,
            """
            SELECT *
            FROM execution_legs
            WHERE workflow_id=?
            ORDER BY leg_index
            """,
            (workflow_id,),
        )

    return _rows(
        con,
        """
        SELECT *
        FROM execution_legs
        WHERE workflow_id=? AND side=?
        ORDER BY leg_index
        """,
        (
            workflow_id,
            side,
        ),
    )


def _decision(
    *,
    workflow_id: str | None,
    action: str,
    reason: str,
    workflow_status: str | None,
    workflow_phase: str | None,
    execution_state: str,
) -> RecoveryDecision:
    return RecoveryDecision(
        workflow_id=workflow_id,
        action=action,
        reason=reason,
        workflow_status=workflow_status,
        workflow_phase=workflow_phase,
        execution_state=execution_state,
        may_submit_order=action in SUBMIT_ACTIONS,
        requires_broker_read=(action in BROKER_READ_ACTIONS),
    )


def discover_recoverable_workflow(
    con: sqlite3.Connection,
) -> str | None:
    rows = _rows(
        con,
        """
        SELECT workflow_id,status,created_at
        FROM execution_workflows
        WHERE status != 'COMPLETE'
        ORDER BY created_at,workflow_id
        """,
    )

    if len(rows) > 1:
        ids = ",".join(str(row["workflow_id"]) for row in rows)
        raise RecoveryInvariantError("multiple non-complete execution workflows: " + ids)

    if not rows:
        state = _machine_state(con)

        if state["execution_state"] != "IDLE":
            raise RecoveryInvariantError("non-IDLE execution state has no recoverable workflow")

        return None

    return str(rows[0]["workflow_id"])


def _assert_leg_shape(
    *,
    phase: str,
    legs: list[dict[str, Any]],
) -> None:
    if not legs:
        raise RecoveryInvariantError(f"{phase} workflow has no {phase} legs")

    for leg in legs:
        if leg["side"] != phase:
            raise RecoveryInvariantError(f"{phase} phase contains non-{phase} leg")

        status = str(leg["status"])
        broker_order_id = leg["broker_order_id"]

        if (
            status
            in {
                "BROKER_ACCEPTED",
                "PENDING",
                "PARTIAL",
                "FILLED",
                "FAILED",
                "UNKNOWN",
            }
            and not broker_order_id
        ):
            raise RecoveryInvariantError(
                f"{phase} leg {leg['leg_index']} has broker-derived status without broker_order_id"
            )

        if status == "PLANNED" and broker_order_id is not None:
            raise RecoveryInvariantError(
                f"{phase} PLANNED leg {leg['leg_index']} already has broker_order_id"
            )


def _ambiguous_intent_exists(
    legs: list[dict[str, Any]],
) -> bool:
    return any(row["status"] == "INTENT_RECORDED" and not row["broker_order_id"] for row in legs)


def _persist_ambiguous_fail_closed(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    created_at: str,
) -> None:
    changed = require_reconciliation_for_ambiguous_intent(
        con,
        workflow_id=workflow_id,
        created_at=created_at,
    )

    if not changed:
        raise RecoveryInvariantError("ambiguous intent disappeared during recovery classification")


def classify_recovery(
    con: sqlite3.Connection,
    *,
    workflow_id: str | None = None,
    created_at: str,
    persist_ambiguous: bool = True,
) -> RecoveryDecision:
    state = _machine_state(con)

    discovered = discover_recoverable_workflow(con)

    if workflow_id is None:
        workflow_id = discovered
    elif discovered is not None and workflow_id != discovered:
        raise RecoveryInvariantError(
            "explicit workflow_id does not match the sole recoverable workflow"
        )
    elif discovered is None:
        explicit = _workflow(con, workflow_id)
        if explicit["status"] != "COMPLETE":
            raise RecoveryInvariantError(
                "explicit non-complete workflow is not the recoverable workflow"
            )

    if workflow_id is None:
        return _decision(
            workflow_id=None,
            action="NO_WORKFLOW",
            reason="execution_idle_no_workflow",
            workflow_status=None,
            workflow_phase=None,
            execution_state=str(state["execution_state"]),
        )

    workflow = _workflow(
        con,
        workflow_id,
    )
    status = str(workflow["status"])
    phase = str(workflow["phase"])
    execution_state = str(state["execution_state"])

    if status == "COMPLETE":
        if execution_state != "IDLE":
            raise RecoveryInvariantError("COMPLETE workflow requires IDLE execution state")

        return _decision(
            workflow_id=workflow_id,
            action="COMPLETE",
            reason="workflow_already_complete",
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if status == "FAILED":
        if execution_state != "FAILED":
            raise RecoveryInvariantError("FAILED workflow requires FAILED execution state")

        return _decision(
            workflow_id=workflow_id,
            action="FAILED",
            reason="workflow_failed_manual_review",
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if status == "RECONCILIATION_REQUIRED" or execution_state == "RECONCILIATION_REQUIRED":
        if not (
            status == "RECONCILIATION_REQUIRED" and execution_state == "RECONCILIATION_REQUIRED"
        ):
            raise RecoveryInvariantError(
                "workflow/machine reconciliation-required state disagreement"
            )

        return _decision(
            workflow_id=workflow_id,
            action="MANUAL_RECONCILIATION",
            reason=("ambiguous_or_unknown_broker_state_automatic_submission_forbidden"),
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if status != "ACTIVE":
        raise RecoveryInvariantError(f"unsupported recoverable workflow status={status}")

    if phase == "NONE":
        if execution_state != "PLAN_CREATED":
            raise RecoveryInvariantError("NONE phase requires PLAN_CREATED")

        if _legs(con, workflow_id, "BUY"):
            raise RecoveryInvariantError("BUY legs cannot exist before workflow start")

        return _decision(
            workflow_id=workflow_id,
            action="START_WORKFLOW",
            reason="workflow_created_but_not_started",
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if phase == "RECONCILE":
        if execution_state != "RECONCILING":
            raise RecoveryInvariantError("RECONCILE phase requires RECONCILING")

        sell_legs = _legs(
            con,
            workflow_id,
            "SELL",
        )

        if sell_legs and any(row["status"] != "FILLED" for row in sell_legs):
            raise RecoveryInvariantError("RECONCILE phase contains non-FILLED SELL leg")

        if _legs(con, workflow_id, "BUY"):
            raise RecoveryInvariantError("RECONCILE phase cannot already contain BUY legs")

        return _decision(
            workflow_id=workflow_id,
            action="REPLAN_BUYS",
            reason=("sell_phase_reconciled_rebuild_buys_from_realized_cash"),
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if phase not in {"SELL", "BUY"}:
        raise RecoveryInvariantError(f"unsupported workflow phase={phase}")

    legs = _legs(
        con,
        workflow_id,
        phase,
    )
    _assert_leg_shape(
        phase=phase,
        legs=legs,
    )

    if _ambiguous_intent_exists(legs):
        if persist_ambiguous:
            _persist_ambiguous_fail_closed(
                con,
                workflow_id=workflow_id,
                created_at=created_at,
            )
            return _decision(
                workflow_id=workflow_id,
                action="MANUAL_RECONCILIATION",
                reason=("durable_intent_without_broker_order_id_no_automatic_retry"),
                workflow_status=("RECONCILIATION_REQUIRED"),
                workflow_phase=phase,
                execution_state=("RECONCILIATION_REQUIRED"),
            )

        return _decision(
            workflow_id=workflow_id,
            action="MANUAL_RECONCILIATION",
            reason=("durable_intent_without_broker_order_id_no_automatic_retry"),
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    statuses = {str(row["status"]) for row in legs}

    if "UNKNOWN" in statuses:
        raise RecoveryInvariantError("ACTIVE workflow cannot contain UNKNOWN leg")

    if "FAILED" in statuses:
        raise RecoveryInvariantError("ACTIVE workflow cannot contain FAILED leg")

    planned = [row for row in legs if row["status"] == "PLANNED"]
    in_flight = [
        row
        for row in legs
        if row["status"]
        in {
            "BROKER_ACCEPTED",
            "PENDING",
            "PARTIAL",
        }
    ]

    if phase == "SELL":
        if execution_state not in {
            "SELL_PENDING",
            "PARTIAL_FILL",
        }:
            raise RecoveryInvariantError(
                f"ACTIVE SELL workflow has incompatible execution_state={execution_state}"
            )

        if planned:
            return _decision(
                workflow_id=workflow_id,
                action="SUBMIT_SELL",
                reason=("known_durable_sell_state_has_unsubmitted_planned_leg"),
                workflow_status=status,
                workflow_phase=phase,
                execution_state=execution_state,
            )

        if in_flight:
            return _decision(
                workflow_id=workflow_id,
                action="RECONCILE_SELL",
                reason=("sell_broker_order_known_never_resubmit_before_reconciliation"),
                workflow_status=status,
                workflow_phase=phase,
                execution_state=execution_state,
            )

        if statuses == {"FILLED"}:
            raise RecoveryInvariantError(
                "SELL legs FILLED but workflow did not advance transactionally"
            )

        raise RecoveryInvariantError("SELL workflow has no safe recovery action")

    if execution_state not in {
        "BUY_PENDING",
        "PARTIAL_FILL",
    }:
        raise RecoveryInvariantError(
            f"ACTIVE BUY workflow has incompatible execution_state={execution_state}"
        )

    if in_flight:
        return _decision(
            workflow_id=workflow_id,
            action="RECONCILE_BUY",
            reason=("buy_broker_order_known_never_resubmit_before_reconciliation"),
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if planned:
        state = _machine_state(con)
        cash = float(state["strategy_cash_eur"])
        debt = float(state["external_cash_debt_eur"])

        if debt > 0.01 or cash < -0.01:
            raise RecoveryInvariantError(
                "PLANNED BUY exists while durable external cash debt remains"
            )

        return _decision(
            workflow_id=workflow_id,
            action="SUBMIT_BUY",
            reason=("no_buy_in_flight_next_planned_buy_may_submit_under_m5b_cash_guard"),
            workflow_status=status,
            workflow_phase=phase,
            execution_state=execution_state,
        )

    if statuses == {"FILLED"}:
        raise RecoveryInvariantError(
            "BUY legs FILLED but workflow did not complete transactionally"
        )

    raise RecoveryInvariantError("BUY workflow has no safe recovery action")
