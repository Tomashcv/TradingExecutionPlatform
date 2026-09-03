from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sp1execution.broker.history_v04 import fetch_history_records
from sp1execution.engine.planner import (
    InstrumentQuote,
    PlannedOrder,
    make_orders,
    position_quantities,
    value_position_eur,
)
from sp1execution.engine.reconciliation_v04 import (
    ReconciledOrder,
    reconcile_accepted_attempts,
)
from sp1execution.execution.workflow_v04 import (
    LegSpec,
    WorkflowSnapshot,
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
from sp1execution.state.capital_v04 import apply_order_fills
from sp1execution.state.v04_store import validate_machine_state

HISTORY_FIRST_PATH = "/equity/history/orders?limit=50"
POSITION_TOLERANCE = 0.00011


class BrokerExecutorError(RuntimeError):
    pass


class BrokerSubmissionAmbiguous(BrokerExecutorError):
    pass


class PhaseNotReady(BrokerExecutorError):
    pass


class PositionReconciliationError(BrokerExecutorError):
    pass


class FreshSellRequired(BrokerExecutorError):
    pass


class CashBoundaryBreach(BrokerExecutorError):
    pass


@dataclass(frozen=True)
class SubmissionResult:
    workflow_id: str
    phase: str
    broker_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    workflow_id: str
    phase: str
    states: tuple[ReconciledOrder, ...]
    snapshot: WorkflowSnapshot


class _CachingHistoryFetcher:
    def __init__(self, fetch: Callable[[str], dict[str, Any]]):
        self.fetch = fetch
        self.cache: dict[str, dict[str, Any]] = {}

    def __call__(self, path: str) -> dict[str, Any]:
        if path not in self.cache:
            payload = self.fetch(path)
            if not isinstance(payload, dict):
                raise TypeError("Trading212 history response must be an object")
            self.cache[path] = payload
        return self.cache[path]


def _rows(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.row_factory = previous


def _row(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    rows = _rows(con, sql, params)
    return None if not rows else rows[0]


def _workflow(con: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
    row = _row(
        con,
        "SELECT * FROM execution_workflows WHERE workflow_id=?",
        (workflow_id,),
    )
    if row is None:
        raise BrokerExecutorError(f"unknown workflow: {workflow_id}")
    return row


def _phase_legs(
    con: sqlite3.Connection,
    workflow_id: str,
    phase: str,
) -> list[dict[str, Any]]:
    return _rows(
        con,
        """
        SELECT *
        FROM execution_legs
        WHERE workflow_id=? AND side=?
        ORDER BY leg_index
        """,
        (workflow_id, phase),
    )


def _all_legs(
    con: sqlite3.Connection,
    workflow_id: str,
) -> list[dict[str, Any]]:
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


def _machine_state(con: sqlite3.Connection) -> dict[str, Any]:
    row = _row(con, "SELECT * FROM machine_state WHERE id=1")
    if row is None:
        raise BrokerExecutorError("machine_state singleton missing")
    return row


def _canonical_source_positions(
    positions: list[dict[str, Any]],
    *,
    strategy_tickers: set[str],
) -> dict[str, float]:
    quantities = position_quantities(positions)
    return {
        ticker: float(quantities.get(ticker, 0.0))
        for ticker in sorted(strategy_tickers)
        if abs(float(quantities.get(ticker, 0.0))) > 1e-12
    }


def _validate_machine_state_compat(
    con: sqlite3.Connection,
) -> dict[str, Any]:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        return validate_machine_state(con)
    finally:
        con.row_factory = previous


def _set_m5b_execution_state(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    event_suffix: str,
    to_state: str,
    reason: str,
    created_at: str,
) -> None:
    state = _machine_state(con)
    from_state = str(state["execution_state"])
    event_key = f"exec:{workflow_id}:m5b:{event_suffix}"
    payload = json.dumps(
        {"workflow_id": workflow_id},
        sort_keys=True,
        separators=(",", ":"),
    )

    prior = con.execute(
        "SELECT dimension,to_state,reason,payload FROM state_transitions WHERE event_key=?",
        (event_key,),
    ).fetchone()

    if prior is not None:
        expected = ("EXECUTION", to_state, reason, payload)
        if tuple(prior) != expected:
            raise BrokerExecutorError(f"conflicting M5B execution transition replay: {event_key}")
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


def _persist_buy_progress(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    leg_statuses: dict[int, str],
    workflow_status: str,
    execution_state: str,
    reason: str,
    event_suffix: str,
    created_at: str,
) -> WorkflowSnapshot:
    if con.in_transaction:
        raise BrokerExecutorError("buy-progress persistence requires no active transaction")

    con.execute("BEGIN IMMEDIATE")
    try:
        workflow = _workflow(con, workflow_id)
        if workflow["phase"] != "BUY":
            raise BrokerExecutorError("buy-progress persistence requires BUY workflow phase")

        for leg_index, status in leg_statuses.items():
            con.execute(
                "UPDATE execution_legs SET status=?,updated_at=? WHERE workflow_id=? AND leg_index=?",
                (status, created_at, workflow_id, int(leg_index)),
            )

        con.execute(
            "UPDATE execution_workflows SET status=?,phase='BUY',updated_at=? WHERE workflow_id=?",
            (workflow_status, created_at, workflow_id),
        )

        _set_m5b_execution_state(
            con,
            workflow_id=workflow_id,
            event_suffix=event_suffix,
            to_state=execution_state,
            reason=reason,
            created_at=created_at,
        )
        _validate_machine_state_compat(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    return WorkflowSnapshot(
        workflow_id,
        workflow_status,
        "BUY",
        execution_state,
    )


def _leg_estimated_notional_eur(leg: dict[str, Any]) -> float:
    payload_text = leg.get("payload")
    if payload_text is None:
        raise BrokerExecutorError(f"execution leg {leg['leg_index']} lacks payload")
    payload = json.loads(payload_text)
    raw = payload.get("estimated_notional_eur")
    if raw is None:
        raise BrokerExecutorError(f"execution leg {leg['leg_index']} lacks EUR notional evidence")
    value = float(raw)
    if value < 0:
        raise BrokerExecutorError("execution leg estimated EUR notional cannot be negative")
    return value


def _strategy_tickers_from_workflow(
    con: sqlite3.Connection,
    workflow_id: str,
) -> set[str]:
    workflow = _workflow(con, workflow_id)
    payload = json.loads(workflow["target_payload"])
    raw = payload.get("strategy_broker_tickers")
    if not isinstance(raw, list) or not raw:
        raise BrokerExecutorError("workflow lacks explicit strategy broker ticker scope")
    return {str(ticker) for ticker in raw}


def workflow_id_for_decision(decision_id: str) -> str:
    if not decision_id:
        raise BrokerExecutorError("decision_id is required")
    return f"m5b:{decision_id}"


def create_and_start_from_fresh_orders(
    con: sqlite3.Connection,
    *,
    decision_id: str,
    decision_payload: dict[str, Any],
    positions: list[dict[str, Any]],
    orders: list[PlannedOrder],
    created_at: str,
) -> WorkflowSnapshot:
    workflow_id = workflow_id_for_decision(decision_id)

    payload_decision_id = decision_payload.get("decision_id")
    if payload_decision_id is not None and str(payload_decision_id) != decision_id:
        raise BrokerExecutorError(
            "decision payload ID does not match requested workflow decision ID"
        )

    raw_strategy_tickers = decision_payload.get("strategy_broker_tickers")
    if not isinstance(raw_strategy_tickers, list) or not raw_strategy_tickers:
        raise BrokerExecutorError("decision payload requires explicit strategy_broker_tickers")
    strategy_tickers = {str(ticker) for ticker in raw_strategy_tickers}

    seen_order_tickers: set[str] = set()
    for row in orders:
        side = str(row.side).upper()
        quantity = float(row.quantity)
        if side not in {"BUY", "SELL"}:
            raise BrokerExecutorError(f"unsupported fresh order side: {row.side!r}")
        if side == "BUY" and quantity <= 0:
            raise BrokerExecutorError("fresh BUY order must have positive quantity")
        if side == "SELL" and quantity >= 0:
            raise BrokerExecutorError("fresh SELL order must have negative quantity")
        if row.broker_ticker not in strategy_tickers:
            raise BrokerExecutorError(
                f"fresh order ticker outside strategy scope: {row.broker_ticker}"
            )
        if row.broker_ticker in seen_order_tickers:
            raise BrokerExecutorError(f"duplicate fresh order ticker: {row.broker_ticker}")
        seen_order_tickers.add(row.broker_ticker)

    initial_plan = [
        {
            "logical_symbol": row.logical_symbol,
            "broker_ticker": row.broker_ticker,
            "quantity": float(row.quantity),
            "side": row.side,
            "estimated_notional_eur": float(row.estimated_notional_eur),
            "delta_eur": float(row.delta_eur),
        }
        for row in orders
    ]

    target_payload = {
        "schema": "m5b_two_phase_broker_executor_v2",
        "decision": decision_payload,
        "target_weights": dict(decision_payload.get("target_weights", {})),
        "strategy_broker_tickers": sorted(strategy_tickers),
        "source_positions_by_ticker": _canonical_source_positions(
            positions,
            strategy_tickers=strategy_tickers,
        ),
        "initial_fresh_plan": initial_plan,
    }

    sell_legs = [
        LegSpec(
            side="SELL",
            logical_symbol=row.logical_symbol,
            broker_ticker=row.broker_ticker,
            quantity=abs(float(row.quantity)),
            estimated_notional_eur=float(row.estimated_notional_eur),
        )
        for row in orders
        if row.side == "SELL"
    ]

    create_workflow(
        con,
        workflow_id=workflow_id,
        decision_id=decision_id,
        target_payload=target_payload,
        sell_legs=sell_legs,
        created_at=created_at,
    )

    return start_workflow(
        con,
        workflow_id=workflow_id,
        created_at=created_at,
    )


def _require_demo_broker(broker: Any) -> None:
    env = getattr(getattr(broker, "settings", None), "t212_env", None)
    if env != "demo":
        raise BrokerExecutorError(
            "M5B broker submission is Demo-only; live submission is prohibited"
        )


def _assert_no_unrelated_pending_orders(
    *,
    broker: Any,
    known_order_ids: set[str],
) -> None:
    pending = broker.pending_orders()
    if not isinstance(pending, list):
        raise BrokerExecutorError("Trading212 pending-orders response must be a list")

    unknown: list[str] = []
    for row in pending:
        if not isinstance(row, dict):
            raise BrokerExecutorError("Trading212 pending order must be an object")
        raw_id = row.get("id")
        if raw_id is None:
            unknown.append("<missing-id>")
            continue
        oid = str(raw_id)
        if oid not in known_order_ids:
            unknown.append(oid)

    if unknown:
        raise BrokerExecutorError(
            "broker has pending orders not owned by this workflow: " + ",".join(unknown)
        )


def _submit_current_phase_unlocked(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    broker: Any,
    now_fn: Callable[[], str],
) -> SubmissionResult:
    _require_demo_broker(broker)

    workflow = _workflow(con, workflow_id)
    if workflow["status"] != "ACTIVE":
        raise PhaseNotReady(f"submission requires ACTIVE workflow, got {workflow['status']}")

    phase = str(workflow["phase"])
    if phase not in {"SELL", "BUY"}:
        raise PhaseNotReady(f"submission requires SELL/BUY phase, got {phase}")

    legs = _phase_legs(con, workflow_id, phase)
    if not legs:
        raise PhaseNotReady(f"no {phase} legs exist")

    if any(row["status"] == "INTENT_RECORDED" and not row["broker_order_id"] for row in legs):
        require_reconciliation_for_ambiguous_intent(
            con,
            workflow_id=workflow_id,
            created_at=now_fn(),
        )
        raise BrokerSubmissionAmbiguous(
            "durable intent exists without broker order ID; automatic retry forbidden"
        )

    known_ids = {
        str(row["broker_order_id"]) for row in _all_legs(con, workflow_id) if row["broker_order_id"]
    }
    _assert_no_unrelated_pending_orders(
        broker=broker,
        known_order_ids=known_ids,
    )

    if phase == "BUY":
        fatal = [row for row in legs if row["status"] in {"UNKNOWN", "FAILED"}]
        if fatal:
            raise PhaseNotReady("BUY submission blocked by terminal/unknown leg state")

        in_flight = [
            row
            for row in legs
            if row["status"] in {"INTENT_RECORDED", "BROKER_ACCEPTED", "PENDING", "PARTIAL"}
        ]
        if in_flight:
            raise PhaseNotReady(
                "BUY leg already in flight; reconcile before submitting another BUY"
            )

        planned = [row for row in legs if row["status"] == "PLANNED"]
        if not planned:
            raise PhaseNotReady("no PLANNED BUY leg remains")

        state = _machine_state(con)
        strategy_cash = float(state["strategy_cash_eur"])
        debt = float(state["external_cash_debt_eur"])

        if debt > 0.01 or strategy_cash < -0.01:
            raise CashBoundaryBreach(
                "BUY submission forbidden while durable external cash debt remains"
            )

        next_leg = planned[0]
        estimated_notional = _leg_estimated_notional_eur(next_leg)
        if estimated_notional > strategy_cash + 0.01:
            raise PhaseNotReady(
                "next BUY estimated notional exceeds durable strategy cash after prior fills"
            )

        candidates = [next_leg]
    else:
        candidates = [row for row in legs if row["status"] == "PLANNED"]

    submitted: list[str] = []

    for leg in candidates:
        leg_index = int(leg["leg_index"])
        created_at = now_fn()

        record_leg_intent(
            con,
            workflow_id=workflow_id,
            leg_index=leg_index,
            created_at=created_at,
        )

        try:
            response = broker.market_order_demo_only(
                leg["broker_ticker"],
                signed_broker_quantity(
                    leg["side"],
                    float(leg["intended_quantity"]),
                ),
            )
        except Exception:
            require_reconciliation_for_ambiguous_intent(
                con,
                workflow_id=workflow_id,
                created_at=now_fn(),
            )
            raise

        broker_order_id = None
        if isinstance(response, dict) and response.get("id") is not None:
            broker_order_id = str(response["id"])

        if not broker_order_id:
            require_reconciliation_for_ambiguous_intent(
                con,
                workflow_id=workflow_id,
                created_at=now_fn(),
            )
            raise BrokerSubmissionAmbiguous(
                "broker response lacks an order ID; automatic retry forbidden"
            )

        record_broker_acceptance(
            con,
            workflow_id=workflow_id,
            leg_index=leg_index,
            broker_order_id=broker_order_id,
            response=response,
            created_at=now_fn(),
        )
        submitted.append(broker_order_id)

    return SubmissionResult(
        workflow_id=workflow_id,
        phase=phase,
        broker_order_ids=tuple(submitted),
    )


def _submission_lock_path(con: sqlite3.Connection) -> str | None:
    import hashlib
    import tempfile
    from pathlib import Path

    database_path = None
    for row in con.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main":
            database_path = str(row[2] or "")
            break

    if not database_path or database_path == ":memory:":
        return None

    canonical = str(Path(database_path).resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return str(Path(tempfile.gettempdir()) / f"sp1execution-submit-{digest}.lock")


def submit_current_phase(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    broker: Any,
    now_fn: Any,
) -> SubmissionResult:
    lock_path = _submission_lock_path(con)
    if lock_path is None:
        return _submit_current_phase_unlocked(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=now_fn,
        )

    try:
        import fcntl
    except ImportError as exc:
        raise BrokerExecutorError(
            "file-backed broker submission requires POSIX flock support"
        ) from exc

    import os

    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    acquired = False

    try:
        try:
            fcntl.flock(
                fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            acquired = True
        except BlockingIOError as exc:
            raise PhaseNotReady(
                "broker submission lock is already held by another cycle invocation"
            ) from exc

        return _submit_current_phase_unlocked(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=now_fn,
        )
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _accepted_attempts(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    phase: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    legs = _phase_legs(con, workflow_id, phase)

    if any(row["status"] == "INTENT_RECORDED" and not row["broker_order_id"] for row in legs):
        raise BrokerSubmissionAmbiguous(
            "intent without broker order ID requires manual reconciliation"
        )

    if phase == "SELL" and any(row["status"] == "PLANNED" for row in legs):
        raise PhaseNotReady("cannot reconcile SELL: not every leg has been submitted")

    submitted_legs = [row for row in legs if row["status"] != "PLANNED"]
    if not submitted_legs:
        raise PhaseNotReady(f"cannot reconcile {phase}: no submitted leg exists")

    attempts: list[dict[str, Any]] = []
    for row in submitted_legs:
        broker_order_id = row["broker_order_id"]
        if not broker_order_id:
            raise BrokerSubmissionAmbiguous(f"{phase} leg {row['leg_index']} lacks broker order ID")
        attempts.append(
            {
                "ticker": row["broker_ticker"],
                "quantity": float(row["intended_quantity"]),
                "side": row["side"],
                "broker_order_id": str(broker_order_id),
            }
        )

    return attempts, submitted_legs


def _expected_positions_after_phase(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    phase: str,
    reconciled_statuses: dict[int, str] | None = None,
) -> dict[str, float]:
    workflow = _workflow(con, workflow_id)
    target_payload = json.loads(workflow["target_payload"])
    source = {
        str(ticker): float(quantity)
        for ticker, quantity in target_payload.get("source_positions_by_ticker", {}).items()
    }

    expected = dict(source)
    statuses = reconciled_statuses or {}

    for leg in _all_legs(con, workflow_id):
        side = str(leg["side"])
        leg_index = int(leg["leg_index"])

        if side == "SELL":
            if phase not in {"SELL", "BUY"}:
                continue
            quantity = -abs(float(leg["intended_quantity"]))
        elif side == "BUY":
            if phase != "BUY":
                continue
            effective_status = statuses.get(leg_index, str(leg["status"]))
            if effective_status != "FILLED":
                continue
            quantity = abs(float(leg["intended_quantity"]))
        else:
            raise BrokerExecutorError(f"unsupported workflow leg side: {side}")

        ticker = str(leg["broker_ticker"])
        expected[ticker] = expected.get(ticker, 0.0) + quantity

    return expected


def _verify_positions(
    *,
    expected: dict[str, float],
    positions: list[dict[str, Any]],
    tolerance: float = POSITION_TOLERANCE,
) -> None:
    actual = position_quantities(positions)

    errors: list[str] = []
    for ticker, expected_quantity in sorted(expected.items()):
        actual_quantity = float(actual.get(ticker, 0.0))
        if abs(actual_quantity - expected_quantity) > tolerance:
            errors.append(
                f"{ticker}: expected={expected_quantity:.8f} actual={actual_quantity:.8f}"
            )

    if errors:
        raise PositionReconciliationError(
            "broker positions do not match filled workflow: " + "; ".join(errors)
        )


def reconcile_current_phase(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    broker: Any,
    created_at: str,
) -> ReconciliationResult:
    _require_demo_broker(broker)

    workflow = _workflow(con, workflow_id)
    if workflow["status"] != "ACTIVE":
        raise PhaseNotReady(f"reconciliation requires ACTIVE workflow, got {workflow['status']}")

    phase = str(workflow["phase"])
    if phase not in {"SELL", "BUY"}:
        raise PhaseNotReady(f"reconciliation requires SELL/BUY phase, got {phase}")

    attempts, submitted_legs = _accepted_attempts(
        con,
        workflow_id=workflow_id,
        phase=phase,
    )

    pending = broker.pending_orders()
    fetcher = _CachingHistoryFetcher(lambda path: broker._request("GET", path))

    states = reconcile_accepted_attempts(
        attempts=attempts,
        pending_payload=pending,
        fetch_history_page=fetcher,
        history_first_path=HISTORY_FIRST_PATH,
    )

    history = fetch_history_records(
        fetcher,
        first_path=HISTORY_FIRST_PATH,
    )

    decision_id = workflow["decision_id"]
    leg_by_order_id = {
        str(row["broker_order_id"]): row for row in submitted_legs if row["broker_order_id"]
    }

    for state in states:
        historical = history.get(state.broker_order_id)
        if historical is None:
            continue
        leg = leg_by_order_id[state.broker_order_id]
        if historical.side.upper() != str(leg["side"]).upper():
            raise BrokerExecutorError(
                f"historical side mismatch for broker order {state.broker_order_id}"
            )
        apply_order_fills(
            con,
            historical,
            decision_id=decision_id,
        )

    leg_index_by_order_id = {
        str(row["broker_order_id"]): int(row["leg_index"]) for row in submitted_legs
    }
    status_by_index = {
        leg_index_by_order_id[state.broker_order_id]: state.state for state in states
    }

    if set(status_by_index) != {int(row["leg_index"]) for row in submitted_legs}:
        raise BrokerExecutorError("reconciliation did not return every submitted phase leg")

    if phase == "SELL":
        all_sell_legs = _phase_legs(con, workflow_id, "SELL")
        if set(status_by_index) != {int(row["leg_index"]) for row in all_sell_legs}:
            raise BrokerExecutorError("SELL reconciliation did not cover every sell leg")

        if set(status_by_index.values()) == {"FILLED"}:
            expected = _expected_positions_after_phase(
                con,
                workflow_id=workflow_id,
                phase="SELL",
                reconciled_statuses=status_by_index,
            )
            _verify_positions(
                expected=expected,
                positions=broker.positions(force_refresh=True),
            )

        snapshot = mark_sell_reconciliation_result(
            con,
            workflow_id=workflow_id,
            leg_statuses=status_by_index,
            created_at=created_at,
        )

        return ReconciliationResult(
            workflow_id=workflow_id,
            phase=phase,
            states=tuple(states),
            snapshot=snapshot,
        )

    # BUY is intentionally sequential.  Only submitted BUY legs are reconciled;
    # later PLANNED BUY legs remain untouched until the current leg is FILLED.
    status_values = set(status_by_index.values())

    if "UNKNOWN" in status_values:
        snapshot = _persist_buy_progress(
            con,
            workflow_id=workflow_id,
            leg_statuses=status_by_index,
            workflow_status="RECONCILIATION_REQUIRED",
            execution_state="RECONCILIATION_REQUIRED",
            reason="unknown_buy_broker_state",
            event_suffix="buy-unknown",
            created_at=created_at,
        )
    elif "FAILED" in status_values:
        snapshot = _persist_buy_progress(
            con,
            workflow_id=workflow_id,
            leg_statuses=status_by_index,
            workflow_status="FAILED",
            execution_state="FAILED",
            reason="buy_order_failed",
            event_suffix="buy-failed",
            created_at=created_at,
        )
    elif "PARTIAL" in status_values:
        snapshot = _persist_buy_progress(
            con,
            workflow_id=workflow_id,
            leg_statuses=status_by_index,
            workflow_status="ACTIVE",
            execution_state="PARTIAL_FILL",
            reason="buy_partial_fill_requires_wait",
            event_suffix="buy-partial",
            created_at=created_at,
        )
    elif status_values != {"FILLED"}:
        snapshot = _persist_buy_progress(
            con,
            workflow_id=workflow_id,
            leg_statuses=status_by_index,
            workflow_status="ACTIVE",
            execution_state="BUY_PENDING",
            reason="buy_order_pending",
            event_suffix="buy-pending",
            created_at=created_at,
        )
    else:
        expected = _expected_positions_after_phase(
            con,
            workflow_id=workflow_id,
            phase="BUY",
            reconciled_statuses=status_by_index,
        )
        _verify_positions(
            expected=expected,
            positions=broker.positions(force_refresh=True),
        )

        state = _machine_state(con)
        strategy_cash = float(state["strategy_cash_eur"])
        debt = float(state["external_cash_debt_eur"])

        if debt > 0.01 or strategy_cash < -0.01:
            _persist_buy_progress(
                con,
                workflow_id=workflow_id,
                leg_statuses=status_by_index,
                workflow_status="RECONCILIATION_REQUIRED",
                execution_state="RECONCILIATION_REQUIRED",
                reason="real_buy_fill_breached_strategy_cash_boundary",
                event_suffix="buy-cash-boundary-breach",
                created_at=created_at,
            )
            raise CashBoundaryBreach(
                "real BUY fill created external cash debt; workflow stopped fail-closed"
            )

        all_buy_legs = _phase_legs(con, workflow_id, "BUY")
        submitted_indexes = set(status_by_index)
        remaining_planned = [
            row
            for row in all_buy_legs
            if int(row["leg_index"]) not in submitted_indexes and row["status"] == "PLANNED"
        ]

        if remaining_planned:
            snapshot = _persist_buy_progress(
                con,
                workflow_id=workflow_id,
                leg_statuses=status_by_index,
                workflow_status="ACTIVE",
                execution_state="BUY_PENDING",
                reason="filled_buy_leg_reconciled_before_next_sequential_buy",
                event_suffix=f"buy-leg-{min(status_by_index)}-filled",
                created_at=created_at,
            )
        else:
            final_statuses = {int(row["leg_index"]): "FILLED" for row in all_buy_legs}
            snapshot = mark_buy_reconciliation_result(
                con,
                workflow_id=workflow_id,
                leg_statuses=final_statuses,
                created_at=created_at,
            )

    return ReconciliationResult(
        workflow_id=workflow_id,
        phase=phase,
        states=tuple(states),
        snapshot=snapshot,
    )


def replan_and_install_buys(
    con: sqlite3.Connection,
    *,
    workflow_id: str,
    target_weights: dict[str, float],
    quotes: dict[str, InstrumentQuote],
    positions: list[dict[str, Any]],
    eurusd: float,
    created_at: str,
    tolerance_fraction_nav: float = 0.0025,
    buy_buffer: float = 0.9975,
) -> tuple[WorkflowSnapshot, list[PlannedOrder]]:
    workflow = _workflow(con, workflow_id)
    state = _machine_state(con)

    if workflow["status"] != "ACTIVE" or workflow["phase"] != "RECONCILE":
        raise PhaseNotReady("fresh BUY replan requires ACTIVE RECONCILE workflow")

    if state["execution_state"] != "RECONCILING":
        raise PhaseNotReady("fresh BUY replan requires execution_state=RECONCILING")

    strategy_tickers = _strategy_tickers_from_workflow(con, workflow_id)
    quote_tickers = {quote.broker_ticker for quote in quotes.values()}

    unknown_quote_tickers = quote_tickers - strategy_tickers
    if unknown_quote_tickers:
        raise BrokerExecutorError(
            "quote universe contains tickers outside strategy scope: "
            + ",".join(sorted(unknown_quote_tickers))
        )

    target_missing_quote = sorted(set(target_weights) - set(quotes))
    if target_missing_quote:
        raise BrokerExecutorError(
            "target weights missing fresh quotes: " + ",".join(target_missing_quote)
        )

    quantities = position_quantities(positions)
    missing_position_quotes = sorted(
        ticker
        for ticker in strategy_tickers
        if abs(float(quantities.get(ticker, 0.0))) > 1e-12 and ticker not in quote_tickers
    )
    if missing_position_quotes:
        raise BrokerExecutorError(
            "strategy position lacks fresh quote: " + ",".join(missing_position_quotes)
        )

    current_values: dict[str, float] = {}
    for logical, quote in quotes.items():
        current_values[logical] = value_position_eur(
            quantities.get(quote.broker_ticker, 0.0),
            quote,
            eurusd,
        )

    invested_value = sum(max(0.0, value) for value in current_values.values())
    strategy_cash = float(state["strategy_cash_eur"])
    nav_eur = invested_value + strategy_cash

    if nav_eur <= 1.0:
        raise BrokerExecutorError(
            f"strategy NAV is not positive after SELL reconciliation: {nav_eur:.2f}"
        )

    orders, _ = make_orders(
        nav_eur=nav_eur,
        target_weights=target_weights,
        quotes=quotes,
        positions=positions,
        eurusd=eurusd,
        tolerance_fraction_nav=tolerance_fraction_nav,
        buy_buffer=buy_buffer,
    )

    fresh_sells = [row for row in orders if row.side == "SELL"]
    if fresh_sells:
        details = ",".join(f"{row.logical_symbol}:{row.quantity:.4f}" for row in fresh_sells)
        raise FreshSellRequired(
            "fresh post-SELL plan still requires SELL orders; BUY phase blocked: " + details
        )

    buy_orders = [row for row in orders if row.side == "BUY"]
    buy_legs = [
        LegSpec(
            side="BUY",
            logical_symbol=row.logical_symbol,
            broker_ticker=row.broker_ticker,
            quantity=abs(float(row.quantity)),
            estimated_notional_eur=float(row.estimated_notional_eur),
        )
        for row in buy_orders
    ]

    snapshot = install_buy_legs(
        con,
        workflow_id=workflow_id,
        buy_legs=buy_legs,
        created_at=created_at,
    )

    return snapshot, buy_orders
