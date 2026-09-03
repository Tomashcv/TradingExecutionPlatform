from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from sp1execution.state.v04_store import validate_machine_state


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegSpec:
    side: str
    logical_symbol: str
    broker_ticker: str
    quantity: float
    estimated_notional_eur: float | None = None


@dataclass(frozen=True)
class WorkflowSnapshot:
    workflow_id: str
    status: str
    phase: str
    execution_state: str


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _with_rows(con: sqlite3.Connection, fn):
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        return fn()
    finally:
        con.row_factory = previous


def _state(con: sqlite3.Connection) -> dict[str, Any]:
    def load():
        row = con.execute("SELECT * FROM machine_state WHERE id=1").fetchone()
        if row is None:
            raise WorkflowError("machine_state singleton missing")
        return dict(row)

    return _with_rows(con, load)


def _workflow(con: sqlite3.Connection, workflow_id: str) -> dict[str, Any] | None:
    def load():
        row = con.execute(
            "SELECT * FROM execution_workflows WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        return None if row is None else dict(row)

    return _with_rows(con, load)


def _legs(con: sqlite3.Connection, workflow_id: str) -> list[dict[str, Any]]:
    def load():
        rows = con.execute(
            "SELECT * FROM execution_legs WHERE workflow_id=? ORDER BY leg_index",
            (workflow_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    return _with_rows(con, load)


def _validate_machine_state(con: sqlite3.Connection) -> None:
    def run():
        validate_machine_state(con)

    _with_rows(con, run)


def _active_workflow_count(con: sqlite3.Connection) -> int:
    return int(
        con.execute("SELECT COUNT(*) FROM execution_workflows WHERE status='ACTIVE'").fetchone()[0]
    )


def _validate_leg(leg: LegSpec, expected_side: str) -> None:
    side = leg.side.upper()
    if side != expected_side:
        raise WorkflowError(f"expected {expected_side} leg, got {side}")
    if not leg.logical_symbol or not leg.broker_ticker:
        raise WorkflowError("leg symbol/ticker required")
    if float(leg.quantity) <= 0:
        raise WorkflowError("leg quantity must be positive magnitude")
    if leg.estimated_notional_eur is not None and float(leg.estimated_notional_eur) < 0:
        raise WorkflowError("estimated_notional_eur cannot be negative")


def _leg_payload(leg: LegSpec) -> str:
    return canonical_json(
        {
            "estimated_notional_eur": leg.estimated_notional_eur,
            "quantity_is_positive_magnitude": True,
        }
    )


def _set_execution_state(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    event_suffix: str,
    to_state: str,
    reason: str,
    created_at: str,
) -> None:
    state = _state(con)
    from_state = str(state["execution_state"])
    event_key = f"exec:{workflow_id}:{event_suffix}"
    payload = canonical_json({"workflow_id": workflow_id})

    prior = con.execute(
        "SELECT dimension,to_state,reason,payload FROM state_transitions WHERE event_key=?",
        (event_key,),
    ).fetchone()
    if prior is not None:
        expected = ("EXECUTION", to_state, reason, payload)
        if tuple(prior) != expected:
            raise WorkflowError(f"conflicting execution transition replay: {event_key}")
        return

    if from_state == to_state:
        return

    revision_before = int(state["revision"])
    revision_after = revision_before + 1

    con.execute(
        "UPDATE machine_state SET revision=?,execution_state=?,updated_at=? WHERE id=1",
        (revision_after, to_state, created_at),
    )
    con.execute(
        "INSERT INTO state_transitions(event_key,revision_before,revision_after,dimension,from_state,to_state,reason,decision_id,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            event_key,
            revision_before,
            revision_after,
            "EXECUTION",
            from_state,
            to_state,
            reason,
            None,
            payload,
            created_at,
        ),
    )


def create_workflow(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    decision_id: str | None,
    target_payload: dict[str, Any],
    sell_legs: list[LegSpec],
    created_at: str,
) -> WorkflowSnapshot:
    if con.in_transaction:
        raise WorkflowError("create_workflow requires no active transaction")
    for leg in sell_legs:
        _validate_leg(leg, "SELL")

    target_text = canonical_json(target_payload)
    existing = _workflow(con, workflow_id)
    if existing is not None:
        actual = [
            (
                row["side"],
                row["logical_symbol"],
                row["broker_ticker"],
                float(row["intended_quantity"]),
                row["payload"],
            )
            for row in _legs(con, workflow_id)
            if row["side"] == "SELL"
        ]
        expected = [
            (
                "SELL",
                leg.logical_symbol,
                leg.broker_ticker,
                float(leg.quantity),
                _leg_payload(leg),
            )
            for leg in sell_legs
        ]
        if (
            existing["decision_id"] != decision_id
            or existing["kind"] != "TWO_PHASE_REBALANCE"
            or existing["target_payload"] != target_text
            or actual != expected
        ):
            raise WorkflowError(f"conflicting workflow replay: {workflow_id}")
        state = _state(con)
        return WorkflowSnapshot(
            workflow_id,
            str(existing["status"]),
            str(existing["phase"]),
            str(state["execution_state"]),
        )

    con.execute("BEGIN IMMEDIATE")
    try:
        state = _state(con)
        if state["execution_state"] != "IDLE":
            raise WorkflowError("new workflow requires execution_state=IDLE")
        if _active_workflow_count(con) != 0:
            raise WorkflowError("another ACTIVE workflow exists")

        con.execute(
            "INSERT INTO execution_workflows(workflow_id,decision_id,kind,status,phase,source_state_revision,target_payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                workflow_id,
                decision_id,
                "TWO_PHASE_REBALANCE",
                "ACTIVE",
                "NONE",
                int(state["revision"]),
                target_text,
                created_at,
                created_at,
            ),
        )

        for index, leg in enumerate(sell_legs):
            con.execute(
                "INSERT INTO execution_legs(workflow_id,leg_index,side,logical_symbol,broker_ticker,intended_quantity,broker_order_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    index,
                    "SELL",
                    leg.logical_symbol,
                    leg.broker_ticker,
                    float(leg.quantity),
                    None,
                    "PLANNED",
                    _leg_payload(leg),
                    created_at,
                    created_at,
                ),
            )

        _set_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix="created",
            to_state="PLAN_CREATED",
            reason="two_phase_workflow_created",
            created_at=created_at,
        )
        _validate_machine_state(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    return WorkflowSnapshot(workflow_id, "ACTIVE", "NONE", "PLAN_CREATED")


def start_workflow(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    created_at: str,
) -> WorkflowSnapshot:
    if con.in_transaction:
        raise WorkflowError("start_workflow requires no active transaction")

    con.execute("BEGIN IMMEDIATE")
    try:
        workflow = _workflow(con, workflow_id)
        if workflow is None:
            raise WorkflowError(f"unknown workflow: {workflow_id}")
        state = _state(con)
        if workflow["status"] != "ACTIVE":
            raise WorkflowError("only ACTIVE workflow can start")
        if workflow["phase"] != "NONE":
            con.commit()
            return WorkflowSnapshot(
                workflow_id,
                str(workflow["status"]),
                str(workflow["phase"]),
                str(state["execution_state"]),
            )
        if state["execution_state"] != "PLAN_CREATED":
            raise WorkflowError("start requires execution_state=PLAN_CREATED")

        sell_count = int(
            con.execute(
                "SELECT COUNT(*) FROM execution_legs WHERE workflow_id=? AND side='SELL'",
                (workflow_id,),
            ).fetchone()[0]
        )
        if sell_count:
            phase = "SELL"
            execution_state = "SELL_PENDING"
            reason = "sell_phase_started"
        else:
            phase = "RECONCILE"
            execution_state = "RECONCILING"
            reason = "no_sell_legs_reconcile_before_buy"

        con.execute(
            "UPDATE execution_workflows SET phase=?,updated_at=? WHERE workflow_id=?",
            (phase, created_at, workflow_id),
        )
        _set_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix="start",
            to_state=execution_state,
            reason=reason,
            created_at=created_at,
        )
        _validate_machine_state(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    return WorkflowSnapshot(workflow_id, "ACTIVE", phase, execution_state)


def record_leg_intent(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    leg_index: int,
    created_at: str,
) -> None:
    workflow = _workflow(con, workflow_id)
    if workflow is None:
        raise WorkflowError(f"unknown workflow: {workflow_id}")
    row = con.execute(
        "SELECT side,status FROM execution_legs WHERE workflow_id=? AND leg_index=?",
        (workflow_id, leg_index),
    ).fetchone()
    if row is None:
        raise WorkflowError(f"unknown leg index: {leg_index}")
    side, status = row
    if workflow["phase"] != side:
        raise WorkflowError(f"{side} intent forbidden during phase={workflow['phase']}")
    if status == "INTENT_RECORDED":
        return
    if status != "PLANNED":
        raise WorkflowError(f"intent requires PLANNED leg, got {status}")
    with con:
        con.execute(
            "UPDATE execution_legs SET status='INTENT_RECORDED',updated_at=? WHERE workflow_id=? AND leg_index=?",
            (created_at, workflow_id, leg_index),
        )


def record_broker_acceptance(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    leg_index: int,
    broker_order_id: str,
    response: dict[str, Any] | None,
    created_at: str,
) -> None:
    if not broker_order_id:
        raise WorkflowError("broker_order_id required")
    row = con.execute(
        "SELECT broker_order_id,status,payload FROM execution_legs WHERE workflow_id=? AND leg_index=?",
        (workflow_id, leg_index),
    ).fetchone()
    if row is None:
        raise WorkflowError(f"unknown leg index: {leg_index}")
    prior_id, status, payload_text = row
    if status in {"BROKER_ACCEPTED", "PENDING", "PARTIAL", "FILLED"}:
        if prior_id != broker_order_id:
            raise WorkflowError("conflicting broker order ID replay")
        return
    if status != "INTENT_RECORDED":
        raise WorkflowError("broker acceptance requires INTENT_RECORDED")

    payload = {} if payload_text is None else json.loads(payload_text)
    payload["broker_acceptance_response"] = response
    with con:
        con.execute(
            "UPDATE execution_legs SET broker_order_id=?,status='BROKER_ACCEPTED',payload=?,updated_at=? WHERE workflow_id=? AND leg_index=?",
            (
                broker_order_id,
                canonical_json(payload),
                created_at,
                workflow_id,
                leg_index,
            ),
        )


def require_reconciliation_for_ambiguous_intent(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    created_at: str,
) -> bool:
    if con.in_transaction:
        raise WorkflowError("ambiguous-intent check requires no active transaction")

    con.execute("BEGIN IMMEDIATE")
    try:
        workflow = _workflow(con, workflow_id)
        if workflow is None:
            raise WorkflowError(f"unknown workflow: {workflow_id}")
        if workflow["status"] != "ACTIVE":
            raise WorkflowError("ambiguous-intent check requires ACTIVE workflow")

        ambiguous = int(
            con.execute(
                "SELECT COUNT(*) FROM execution_legs WHERE workflow_id=? AND status='INTENT_RECORDED' AND broker_order_id IS NULL",
                (workflow_id,),
            ).fetchone()[0]
        )
        if not ambiguous:
            con.commit()
            return False

        con.execute(
            "UPDATE execution_workflows SET status='RECONCILIATION_REQUIRED',updated_at=? WHERE workflow_id=?",
            (created_at, workflow_id),
        )
        _set_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix="ambiguous-intent",
            to_state="RECONCILIATION_REQUIRED",
            reason="submit_may_have_reached_broker_without_acceptance_record",
            created_at=created_at,
        )
        _validate_machine_state(con)
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise


def _assert_reconcilable_legs(
    legs: list[dict[str, Any]],
) -> None:
    allowed_prior = {
        "BROKER_ACCEPTED",
        "PENDING",
        "PARTIAL",
        "FILLED",
    }

    if not legs:
        raise WorkflowError("reconciliation requires at least one broker leg")

    for leg in legs:
        if not leg["broker_order_id"]:
            raise WorkflowError("reconciliation requires a durable broker_order_id")
        if leg["status"] not in allowed_prior:
            raise WorkflowError("reconciliation requires a previously accepted broker order")


def mark_sell_reconciliation_result(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    leg_statuses: dict[int, str],
    created_at: str,
) -> WorkflowSnapshot:
    allowed = {"PENDING", "PARTIAL", "FILLED", "FAILED", "UNKNOWN"}
    if any(status not in allowed for status in leg_statuses.values()):
        raise WorkflowError("unsupported reconciliation status")
    if con.in_transaction:
        raise WorkflowError("reconciliation result requires no active transaction")

    con.execute("BEGIN IMMEDIATE")
    try:
        workflow = _workflow(con, workflow_id)
        if workflow is None or workflow["status"] != "ACTIVE" or workflow["phase"] != "SELL":
            raise WorkflowError("SELL reconciliation requires ACTIVE SELL workflow")

        sell_legs = [row for row in _legs(con, workflow_id) if row["side"] == "SELL"]
        _assert_reconcilable_legs(sell_legs)

        if set(leg_statuses) != {int(row["leg_index"]) for row in sell_legs}:
            raise WorkflowError("SELL reconciliation must cover every sell leg")

        for leg in sell_legs:
            con.execute(
                "UPDATE execution_legs SET status=?,updated_at=? WHERE workflow_id=? AND leg_index=?",
                (
                    leg_statuses[int(leg["leg_index"])],
                    created_at,
                    workflow_id,
                    leg["leg_index"],
                ),
            )

        statuses = set(leg_statuses.values())
        if "UNKNOWN" in statuses:
            wf_status, phase, exec_state, reason = (
                "RECONCILIATION_REQUIRED",
                "SELL",
                "RECONCILIATION_REQUIRED",
                "unknown_sell_broker_state",
            )
        elif "FAILED" in statuses:
            wf_status, phase, exec_state, reason = (
                "FAILED",
                "SELL",
                "FAILED",
                "sell_order_failed",
            )
        elif "PARTIAL" in statuses:
            wf_status, phase, exec_state, reason = (
                "ACTIVE",
                "SELL",
                "PARTIAL_FILL",
                "sell_partial_fill_blocks_buy",
            )
        elif statuses == {"FILLED"}:
            wf_status, phase, exec_state, reason = (
                "ACTIVE",
                "RECONCILE",
                "RECONCILING",
                "all_sell_legs_filled_replan_buys",
            )
        else:
            wf_status, phase, exec_state, reason = (
                "ACTIVE",
                "SELL",
                "SELL_PENDING",
                "sell_orders_pending",
            )

        con.execute(
            "UPDATE execution_workflows SET status=?,phase=?,updated_at=? WHERE workflow_id=?",
            (wf_status, phase, created_at, workflow_id),
        )
        _set_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix=f"sell-reconcile-{exec_state.lower()}",
            to_state=exec_state,
            reason=reason,
            created_at=created_at,
        )
        _validate_machine_state(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    return WorkflowSnapshot(workflow_id, wf_status, phase, exec_state)


def install_buy_legs(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    buy_legs: list[LegSpec],
    created_at: str,
) -> WorkflowSnapshot:
    for leg in buy_legs:
        _validate_leg(leg, "BUY")
        if leg.estimated_notional_eur is None:
            raise WorkflowError("BUY leg requires estimated_notional_eur")

    if con.in_transaction:
        raise WorkflowError("install_buy_legs requires no active transaction")

    con.execute("BEGIN IMMEDIATE")
    try:
        workflow = _workflow(con, workflow_id)
        state = _state(con)
        if workflow is None or workflow["status"] != "ACTIVE" or workflow["phase"] != "RECONCILE":
            raise WorkflowError("BUY installation requires ACTIVE RECONCILE workflow")
        if state["execution_state"] != "RECONCILING":
            raise WorkflowError("BUY installation requires execution_state=RECONCILING")

        sell_statuses = {row["status"] for row in _legs(con, workflow_id) if row["side"] == "SELL"}
        if sell_statuses and sell_statuses != {"FILLED"}:
            raise WorkflowError("BUY forbidden until every SELL leg is FILLED")
        if any(row["side"] == "BUY" for row in _legs(con, workflow_id)):
            raise WorkflowError("BUY legs already installed")

        if not buy_legs:
            con.execute(
                "UPDATE execution_workflows SET status='COMPLETE',phase='RECONCILE',updated_at=? WHERE workflow_id=?",
                (created_at, workflow_id),
            )
            _set_execution_state(
                con,
                workflow_id=workflow_id,
                event_suffix="no-buy-complete",
                to_state="IDLE",
                reason="no_buy_legs_workflow_complete",
                created_at=created_at,
            )
            _validate_machine_state(con)
            con.commit()
            return WorkflowSnapshot(workflow_id, "COMPLETE", "RECONCILE", "IDLE")

        strategy_cash_eur = float(state["strategy_cash_eur"])
        external_cash_debt_eur = float(state["external_cash_debt_eur"])

        if external_cash_debt_eur > 0.01 or strategy_cash_eur < -0.01:
            raise WorkflowError("BUY forbidden while external cash debt remains")

        planned = sum(float(leg.estimated_notional_eur) for leg in buy_legs)
        if planned > strategy_cash_eur + 0.01:
            raise WorkflowError("planned BUY notional exceeds realized strategy cash")

        next_index = int(
            con.execute(
                "SELECT COALESCE(MAX(leg_index),-1)+1 FROM execution_legs WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()[0]
        )
        for offset, leg in enumerate(buy_legs):
            con.execute(
                "INSERT INTO execution_legs(workflow_id,leg_index,side,logical_symbol,broker_ticker,intended_quantity,broker_order_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    next_index + offset,
                    "BUY",
                    leg.logical_symbol,
                    leg.broker_ticker,
                    float(leg.quantity),
                    None,
                    "PLANNED",
                    _leg_payload(leg),
                    created_at,
                    created_at,
                ),
            )

        con.execute(
            "UPDATE execution_workflows SET phase='BUY',updated_at=? WHERE workflow_id=?",
            (created_at, workflow_id),
        )
        _set_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix="buy-installed",
            to_state="BUY_PENDING",
            reason="buy_phase_installed_from_realized_strategy_cash",
            created_at=created_at,
        )
        _validate_machine_state(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    return WorkflowSnapshot(workflow_id, "ACTIVE", "BUY", "BUY_PENDING")


def mark_buy_reconciliation_result(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    leg_statuses: dict[int, str],
    created_at: str,
) -> WorkflowSnapshot:
    allowed = {"PENDING", "PARTIAL", "FILLED", "FAILED", "UNKNOWN"}
    if any(status not in allowed for status in leg_statuses.values()):
        raise WorkflowError("unsupported reconciliation status")
    if con.in_transaction:
        raise WorkflowError("BUY reconciliation requires no active transaction")

    con.execute("BEGIN IMMEDIATE")
    try:
        workflow = _workflow(con, workflow_id)
        if workflow is None or workflow["status"] != "ACTIVE" or workflow["phase"] != "BUY":
            raise WorkflowError("BUY reconciliation requires ACTIVE BUY workflow")

        buy_legs = [row for row in _legs(con, workflow_id) if row["side"] == "BUY"]
        _assert_reconcilable_legs(buy_legs)

        if set(leg_statuses) != {int(row["leg_index"]) for row in buy_legs}:
            raise WorkflowError("BUY reconciliation must cover every buy leg")

        for leg in buy_legs:
            con.execute(
                "UPDATE execution_legs SET status=?,updated_at=? WHERE workflow_id=? AND leg_index=?",
                (
                    leg_statuses[int(leg["leg_index"])],
                    created_at,
                    workflow_id,
                    leg["leg_index"],
                ),
            )

        statuses = set(leg_statuses.values())
        if "UNKNOWN" in statuses:
            wf_status, phase, exec_state, reason = (
                "RECONCILIATION_REQUIRED",
                "BUY",
                "RECONCILIATION_REQUIRED",
                "unknown_buy_broker_state",
            )
        elif "FAILED" in statuses:
            wf_status, phase, exec_state, reason = (
                "FAILED",
                "BUY",
                "FAILED",
                "buy_order_failed",
            )
        elif "PARTIAL" in statuses:
            wf_status, phase, exec_state, reason = (
                "ACTIVE",
                "BUY",
                "PARTIAL_FILL",
                "buy_partial_fill_requires_wait",
            )
        elif statuses == {"FILLED"}:
            wf_status, phase, exec_state, reason = (
                "COMPLETE",
                "RECONCILE",
                "IDLE",
                "all_buy_legs_filled_workflow_complete",
            )
        else:
            wf_status, phase, exec_state, reason = (
                "ACTIVE",
                "BUY",
                "BUY_PENDING",
                "buy_orders_pending",
            )

        con.execute(
            "UPDATE execution_workflows SET status=?,phase=?,updated_at=? WHERE workflow_id=?",
            (wf_status, phase, created_at, workflow_id),
        )
        _set_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix=f"buy-reconcile-{exec_state.lower()}",
            to_state=exec_state,
            reason=reason,
            created_at=created_at,
        )
        _validate_machine_state(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    return WorkflowSnapshot(workflow_id, wf_status, phase, exec_state)


def signed_broker_quantity(side: str, quantity: float) -> float:
    magnitude = abs(float(quantity))
    if side.upper() == "BUY":
        return magnitude
    if side.upper() == "SELL":
        return -magnitude
    raise WorkflowError(f"unsupported side: {side}")
