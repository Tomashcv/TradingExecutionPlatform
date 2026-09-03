from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from sp1execution.state.v04_store import validate_machine_state

TOL = 1e-12

DIMENSION_COLUMN = {
    "ENTRY": "entry_state",
    "STRATEGY": "strategy_state",
    "MEMBERSHIP": "membership_state",
}

ALLOWED_TRANSITIONS = {
    "ENTRY": {
        "UNINITIALIZED": {"WAIT_CASH", "ENTRY_COMPLETE"},
        "WAIT_CASH": {"CRASH_BUY"},
        "CRASH_BUY": {"HANDOFF_TO_SP2"},
        "HANDOFF_TO_SP2": {"ENTRY_COMPLETE"},
        "ENTRY_COMPLETE": set(),
    },
    "STRATEGY": {
        "INACTIVE": {"NORMAL"},
        "NORMAL": {"CRASH"},
        "CRASH": {"POST_HANDOFF"},
        "POST_HANDOFF": {"NORMAL"},
    },
    "MEMBERSHIP": {
        "UNINITIALIZED": {"ACTIVE"},
        "ACTIVE": {"MONTH_END_PENDING"},
        "MONTH_END_PENDING": {"ACTIVE", "REBALANCE_PENDING"},
        "REBALANCE_PENDING": {"ACTIVE"},
    },
}

PATCHABLE_FIELDS = {
    "entry_policy",
    "strategy_state",
    "active_membership_month",
    "active_membership_json",
    "active_overlay",
    "sp2_mix_json",
    "old_peak",
    "trough",
    "rearm_old_ath",
    "marked_nav_eur",
}


class StateTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransitionResult:
    event_key: str
    status: str
    dimension: str
    from_state: str | None
    to_state: str
    revision_before: int | None
    revision_after: int


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _load_machine_state(con: sqlite3.Connection) -> dict[str, Any]:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM machine_state WHERE id=1").fetchone()
    finally:
        con.row_factory = previous

    if row is None:
        raise StateTransitionError("machine_state singleton missing")

    return dict(row)


def _validate_machine_state_compat(
    con: sqlite3.Connection,
) -> dict[str, Any]:
    previous = con.row_factory
    con.row_factory = sqlite3.Row

    try:
        return validate_machine_state(con)
    finally:
        con.row_factory = previous


def _active_workflow_count(con: sqlite3.Connection) -> int:
    return int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM execution_workflows
            WHERE status='ACTIVE'
            """
        ).fetchone()[0]
    )


def _existing_transition(
    con: sqlite3.Connection,
    event_key: str,
) -> dict[str, Any] | None:
    previous = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT *
            FROM state_transitions
            WHERE event_key=?
            """,
            (event_key,),
        ).fetchone()
    finally:
        con.row_factory = previous

    return _row_dict(row)


def _validate_membership_payload(
    membership: dict[str, Any],
) -> tuple[str, tuple[str, str]]:
    month = membership.get("month")
    symbols = membership.get("symbols")

    if not isinstance(month, str) or len(month) != 7:
        raise StateTransitionError("membership month must be YYYY-MM")

    if (
        not isinstance(symbols, list)
        or len(symbols) != 2
        or not all(isinstance(x, str) and x for x in symbols)
        or len(set(symbols)) != 2
    ):
        raise StateTransitionError("membership must contain exactly two distinct symbols")

    return month, (symbols[0], symbols[1])


def classify_membership_candidate(
    *,
    active_membership: dict[str, Any],
    candidate_membership: dict[str, Any],
) -> str:
    _, active = _validate_membership_payload(active_membership)
    _, candidate = _validate_membership_payload(candidate_membership)

    if set(active) == set(candidate):
        return "SAME_SET_NO_TRADE"

    return "SET_CHANGE_REBALANCE"


def _validate_patch_target(
    target: dict[str, Any],
) -> None:
    overlay = target.get("active_overlay")
    if overlay is not None:
        overlay = float(overlay)
        if overlay < -TOL or overlay > 1.0 + TOL:
            raise StateTransitionError(f"active_overlay out of range: {overlay}")

    membership_raw = target.get("active_membership_json")
    membership = None

    if membership_raw is not None:
        try:
            membership = json.loads(membership_raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StateTransitionError("active_membership_json must be valid JSON") from exc

        month, symbols = _validate_membership_payload(membership)

        if target.get("active_membership_month") != month:
            raise StateTransitionError("active membership month/json mismatch")

        mix_raw = target.get("sp2_mix_json")
        if mix_raw is not None:
            try:
                mix = json.loads(mix_raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise StateTransitionError("sp2_mix_json must be valid JSON") from exc

            if set(mix) != set(symbols):
                raise StateTransitionError("sp2_mix symbols must equal active membership symbols")

            total = sum(float(value) for value in mix.values())
            if abs(total - 1.0) > 1e-8:
                raise StateTransitionError(f"sp2_mix must sum to 1.0, got {total}")

    entry = target["entry_state"]
    strategy = target["strategy_state"]

    if entry != "ENTRY_COMPLETE" and strategy != "INACTIVE":
        raise StateTransitionError("non-complete entry requires strategy_state=INACTIVE")

    if entry == "ENTRY_COMPLETE" and membership is None:
        raise StateTransitionError("ENTRY_COMPLETE requires active membership")

    if strategy == "CRASH":
        if overlay is None or overlay <= TOL:
            raise StateTransitionError("CRASH requires positive active_overlay")

        if target.get("old_peak") is None or target.get("trough") is None:
            raise StateTransitionError("CRASH requires old_peak and trough")

    if strategy == "POST_HANDOFF":
        if overlay is None or abs(float(overlay)) > TOL:
            raise StateTransitionError("POST_HANDOFF requires zero active_overlay")

        if (
            target.get("old_peak") is None
            or target.get("trough") is None
            or target.get("rearm_old_ath") is None
        ):
            raise StateTransitionError("POST_HANDOFF requires old_peak/trough/rearm_old_ath")

    if strategy == "NORMAL" and overlay is not None and abs(float(overlay)) > TOL:
        raise StateTransitionError("NORMAL requires zero active_overlay")


def transition_state(
    con: sqlite3.Connection,
    *,
    event_key: str,
    dimension: str,
    to_state: str,
    reason: str,
    decision_id: str | None = None,
    payload: dict[str, Any] | None = None,
    updates: dict[str, Any] | None = None,
    allow_same_state: bool = False,
    created_at: str,
) -> TransitionResult:
    if con.in_transaction:
        raise StateTransitionError("transition_state requires no active transaction")

    dimension = dimension.upper()

    if dimension not in DIMENSION_COLUMN:
        raise StateTransitionError(f"unsupported transition dimension: {dimension}")

    if not event_key:
        raise StateTransitionError("event_key is required")

    if not reason:
        raise StateTransitionError("reason is required")

    payload_text = canonical_json(payload or {})
    updates = dict(updates or {})

    unknown_updates = set(updates) - PATCHABLE_FIELDS
    if unknown_updates:
        raise StateTransitionError(
            f"unsupported machine-state patch fields: {sorted(unknown_updates)}"
        )

    con.execute("BEGIN IMMEDIATE")

    try:
        existing = _existing_transition(con, event_key)

        if existing is not None:
            expected = {
                "dimension": dimension,
                "to_state": to_state,
                "reason": reason,
                "decision_id": decision_id,
                "payload": payload_text,
            }

            for key, value in expected.items():
                if existing[key] != value:
                    raise StateTransitionError(
                        f"conflicting replay for transition {event_key}: "
                        f"{key}={existing[key]!r} expected={value!r}"
                    )

            con.commit()

            return TransitionResult(
                event_key=event_key,
                status="ALREADY_APPLIED",
                dimension=dimension,
                from_state=existing["from_state"],
                to_state=existing["to_state"],
                revision_before=existing["revision_before"],
                revision_after=int(existing["revision_after"]),
            )

        state = _load_machine_state(con)
        column = DIMENSION_COLUMN[dimension]
        from_state = str(state[column])
        revision_before = int(state["revision"])

        allowed = ALLOWED_TRANSITIONS[dimension].get(from_state)
        if allowed is None:
            raise StateTransitionError(f"unknown {dimension} from_state={from_state}")

        if to_state == from_state:
            if not allow_same_state:
                raise StateTransitionError(
                    f"same-state transition not allowed: {dimension} {from_state}->{to_state}"
                )
        elif to_state not in allowed:
            raise StateTransitionError(f"illegal transition: {dimension} {from_state}->{to_state}")

        if state["execution_state"] != "IDLE":
            raise StateTransitionError("control-state transition requires execution_state=IDLE")

        if _active_workflow_count(con) != 0:
            raise StateTransitionError(
                "control-state transition blocked by active execution workflow"
            )

        target = dict(state)
        target[column] = to_state

        for key, value in updates.items():
            if key in {
                "active_membership_json",
                "sp2_mix_json",
            } and isinstance(value, dict):
                value = canonical_json(value)

            target[key] = value

        _validate_patch_target(target)

        revision_after = revision_before + 1

        assignments = [
            f"{column}=?",
            "revision=?",
            "updated_at=?",
        ]
        values: list[Any] = [
            to_state,
            revision_after,
            created_at,
        ]

        for key in sorted(updates):
            value = target[key]
            assignments.append(f"{key}=?")
            values.append(value)

        values.append(1)

        con.execute(
            f"""
            UPDATE machine_state
            SET {", ".join(assignments)}
            WHERE id=?
            """,
            tuple(values),
        )

        con.execute(
            """
            INSERT INTO state_transitions(
                event_key,
                revision_before,
                revision_after,
                dimension,
                from_state,
                to_state,
                reason,
                decision_id,
                payload,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_key,
                revision_before,
                revision_after,
                dimension,
                from_state,
                to_state,
                reason,
                decision_id,
                payload_text,
                created_at,
            ),
        )

        _validate_machine_state_compat(con)
        con.commit()

        return TransitionResult(
            event_key=event_key,
            status="APPLIED",
            dimension=dimension,
            from_state=from_state,
            to_state=to_state,
            revision_before=revision_before,
            revision_after=revision_after,
        )

    except Exception:
        con.rollback()
        raise


def apply_entry_transition(
    con: sqlite3.Connection,
    *,
    event_key: str,
    to_state: str,
    reason: str,
    created_at: str,
    decision_id: str | None = None,
) -> TransitionResult:
    state = _load_machine_state(con)
    updates: dict[str, Any] = {}
    payload: dict[str, Any] = {}

    if state["entry_state"] == "UNINITIALIZED" and to_state == "WAIT_CASH":
        updates["entry_policy"] = "WAIT_CASH"

    elif state["entry_state"] == "UNINITIALIZED" and to_state == "ENTRY_COMPLETE":
        updates["entry_policy"] = "IMMEDIATE_SP2"

    if to_state == "ENTRY_COMPLETE" and state["strategy_state"] == "INACTIVE":
        updates["strategy_state"] = "NORMAL"
        payload["coupled_strategy_activation"] = {
            "from": "INACTIVE",
            "to": "NORMAL",
            "reason": "entry_completion_requires_active_strategy",
        }

    return transition_state(
        con,
        event_key=event_key,
        dimension="ENTRY",
        to_state=to_state,
        reason=reason,
        decision_id=decision_id,
        payload=payload,
        updates=updates,
        created_at=created_at,
    )


def apply_strategy_transition(
    con: sqlite3.Connection,
    *,
    event_key: str,
    to_state: str,
    reason: str,
    active_overlay: float,
    old_peak: float | None,
    trough: float | None,
    rearm_old_ath: float | None,
    created_at: str,
    decision_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> TransitionResult:
    updates = {
        "active_overlay": float(active_overlay),
        "old_peak": old_peak,
        "trough": trough,
        "rearm_old_ath": rearm_old_ath,
    }

    return transition_state(
        con,
        event_key=event_key,
        dimension="STRATEGY",
        to_state=to_state,
        reason=reason,
        decision_id=decision_id,
        payload=payload or {},
        updates=updates,
        allow_same_state=True,
        created_at=created_at,
    )


def begin_month_end(
    con: sqlite3.Connection,
    *,
    event_key: str,
    candidate_membership: dict[str, Any],
    created_at: str,
) -> TransitionResult:
    _validate_membership_payload(candidate_membership)

    return transition_state(
        con,
        event_key=event_key,
        dimension="MEMBERSHIP",
        to_state="MONTH_END_PENDING",
        reason="monthly_membership_candidate_observed",
        payload={
            "candidate_membership": candidate_membership,
        },
        created_at=created_at,
    )


def classify_month_end(
    con: sqlite3.Connection,
    *,
    event_key: str,
    candidate_membership: dict[str, Any],
    created_at: str,
) -> TransitionResult:
    state = _load_machine_state(con)

    if state["active_membership_json"] is None:
        raise StateTransitionError("cannot classify month-end without active membership")

    active = json.loads(state["active_membership_json"])

    classification = classify_membership_candidate(
        active_membership=active,
        candidate_membership=candidate_membership,
    )

    candidate_month, _ = _validate_membership_payload(candidate_membership)

    if classification == "SAME_SET_NO_TRADE":
        updates = {
            "active_membership_month": candidate_month,
            "active_membership_json": candidate_membership,
        }
        to_state = "ACTIVE"
        reason = "monthly_same_top2_set_no_trade"
    else:
        updates = {}
        to_state = "REBALANCE_PENDING"
        reason = "monthly_top2_set_change_requires_rebalance"

    return transition_state(
        con,
        event_key=event_key,
        dimension="MEMBERSHIP",
        to_state=to_state,
        reason=reason,
        payload={
            "classification": classification,
            "candidate_membership": candidate_membership,
        },
        updates=updates,
        created_at=created_at,
    )


def commit_membership_rebalance(
    con: sqlite3.Connection,
    *,
    event_key: str,
    new_membership: dict[str, Any],
    created_at: str,
    decision_id: str,
) -> TransitionResult:
    month, symbols = _validate_membership_payload(new_membership)

    return transition_state(
        con,
        event_key=event_key,
        dimension="MEMBERSHIP",
        to_state="ACTIVE",
        reason="membership_rebalance_fully_reconciled",
        decision_id=decision_id,
        payload={
            "new_membership": new_membership,
        },
        updates={
            "active_membership_month": month,
            "active_membership_json": new_membership,
            "sp2_mix_json": {
                symbols[0]: 0.5,
                symbols[1]: 0.5,
            },
        },
        created_at=created_at,
    )
