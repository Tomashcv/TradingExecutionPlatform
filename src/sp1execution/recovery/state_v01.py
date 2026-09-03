from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sp1execution.recovery.core_return_v01 import validate_frozen_target


SCHEMA_VERSION = "0.1.0"
TOL = 1e-9

RECOVERY_PHASES = (
    "NORMAL",
    "WAIT_D40",
    "RECOVERY_ACTIVE",
    "OLD_ATH_GUARD",
)

PENDING_EVENT_STATUSES = (
    "PENDING",
    "MATURED",
    "APPLIED",
    "CANCELLED",
)

ALLOWED_PHASE_TRANSITIONS = {
    "NORMAL": {"WAIT_D40"},
    "WAIT_D40": {
        "WAIT_D40",
        "RECOVERY_ACTIVE",
        "NORMAL",
    },
    "RECOVERY_ACTIVE": {
        "RECOVERY_ACTIVE",
        "OLD_ATH_GUARD",
    },
    "OLD_ATH_GUARD": {
        "NORMAL",
    },
}

ALLOWED_EVENT_STATUS_TRANSITIONS = {
    "PENDING": {
        "MATURED",
        "CANCELLED",
    },
    "MATURED": {
        "APPLIED",
        "CANCELLED",
    },
    "APPLIED": set(),
    "CANCELLED": set(),
}


class RecoveryStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryTransitionResult:
    event_key: str
    status: str
    from_phase: str
    to_phase: str
    revision_before: int
    revision_after: int


@dataclass(frozen=True)
class ReserveLedgerResult:
    event_key: str
    status: str
    old_balance_eur: float
    new_balance_eur: float
    revision_before: int
    revision_after: int


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join(
        "'" + value.replace("'", "''") + "'"
        for value in values
    )


def _fetchone_dict(
    con: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row

    try:
        row = con.execute(
            sql,
            params,
        ).fetchone()
    finally:
        con.row_factory = old_factory

    return None if row is None else dict(row)


def ensure_recovery_schema(
    con: sqlite3.Connection,
) -> None:
    phases = _sql_values(
        RECOVERY_PHASES
    )

    statuses = _sql_values(
        PENDING_EVENT_STATUSES
    )

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS recovery_state_v01 (
            id INTEGER PRIMARY KEY CHECK (id = 1),

            schema_version TEXT NOT NULL,

            revision INTEGER NOT NULL
                CHECK (revision >= 1),

            phase TEXT NOT NULL
                CHECK (phase IN ({phases})),

            cycle_id TEXT,

            old_ath REAL,

            current_target REAL NOT NULL
                CHECK (
                    current_target IN (
                        0.0,
                        0.10,
                        0.30,
                        0.60,
                        1.0
                    )
                ),

            first_actual_entry_session TEXT,

            fixed_exit_session TEXT,

            old_ath_recovered INTEGER NOT NULL
                CHECK (
                    old_ath_recovered IN (0,1)
                ),

            reserve_bucket_eur REAL NOT NULL
                CHECK (
                    reserve_bucket_eur >= 0.0
                ),

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS recovery_pending_events_v01 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            source_event_key TEXT NOT NULL UNIQUE,

            cycle_id TEXT NOT NULL,

            source_signal_session TEXT,

            source_execution_session TEXT NOT NULL,

            maturity_session TEXT NOT NULL,

            target REAL NOT NULL
                CHECK (
                    target IN (
                        0.10,
                        0.30,
                        0.60,
                        1.0
                    )
                ),

            status TEXT NOT NULL
                CHECK (
                    status IN ({statuses})
                ),

            payload TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_recovery_pending_cycle_status
        ON recovery_pending_events_v01(
            cycle_id,
            status,
            maturity_session
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS recovery_transitions_v01 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            event_key TEXT NOT NULL UNIQUE,

            revision_before INTEGER NOT NULL,
            revision_after INTEGER NOT NULL,

            from_phase TEXT NOT NULL,
            to_phase TEXT NOT NULL,

            reason TEXT NOT NULL,

            updates_json TEXT NOT NULL,

            payload TEXT,

            created_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS recovery_reserve_ledger_v01 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            event_key TEXT NOT NULL UNIQUE,

            amount_eur REAL NOT NULL,

            reason TEXT NOT NULL,

            balance_before_eur REAL NOT NULL
                CHECK (
                    balance_before_eur >= 0.0
                ),

            balance_after_eur REAL NOT NULL
                CHECK (
                    balance_after_eur >= 0.0
                ),

            revision_before INTEGER NOT NULL,
            revision_after INTEGER NOT NULL,

            payload TEXT,

            created_at TEXT NOT NULL
        )
        """
    )


def initialize_recovery_state(
    con: sqlite3.Connection,
    *,
    reserve_bucket_eur: float = 0.0,
) -> dict[str, Any]:
    reserve = float(
        reserve_bucket_eur
    )

    if reserve < -TOL:
        raise RecoveryStateError(
            "initial reserve bucket cannot be negative"
        )

    reserve = max(
        0.0,
        reserve,
    )

    ensure_recovery_schema(
        con
    )

    existing = _fetchone_dict(
        con,
        """
        SELECT *
        FROM recovery_state_v01
        WHERE id=1
        """,
    )

    if existing is not None:
        validate_recovery_state(
            con
        )
        return existing

    now = now_utc_iso()

    con.execute(
        """
        INSERT INTO recovery_state_v01(
            id,
            schema_version,
            revision,
            phase,
            cycle_id,
            old_ath,
            current_target,
            first_actual_entry_session,
            fixed_exit_session,
            old_ath_recovered,
            reserve_bucket_eur,
            created_at,
            updated_at
        )
        VALUES(
            1,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            SCHEMA_VERSION,
            1,
            "NORMAL",
            None,
            None,
            0.0,
            None,
            None,
            0,
            reserve,
            now,
            now,
        ),
    )

    con.commit()

    return load_recovery_state(
        con
    )


def load_recovery_state(
    con: sqlite3.Connection,
) -> dict[str, Any]:
    row = _fetchone_dict(
        con,
        """
        SELECT *
        FROM recovery_state_v01
        WHERE id=1
        """,
    )

    if row is None:
        raise RecoveryStateError(
            "recovery_state_v01 singleton missing"
        )

    return row


def _validate_state_dict(
    state: dict[str, Any],
) -> None:
    if (
        state["schema_version"]
        !=
        SCHEMA_VERSION
    ):
        raise RecoveryStateError(
            "unexpected recovery schema version"
        )

    if (
        state["phase"]
        not in
        RECOVERY_PHASES
    ):
        raise RecoveryStateError(
            "invalid recovery phase"
        )

    if int(
        state["revision"]
    ) < 1:
        raise RecoveryStateError(
            "invalid recovery revision"
        )

    target = validate_frozen_target(
        float(
            state["current_target"]
        )
    )

    reserve = float(
        state[
            "reserve_bucket_eur"
        ]
    )

    if reserve < -TOL:
        raise RecoveryStateError(
            "negative reserve bucket"
        )

    phase = str(
        state["phase"]
    )

    cycle_id = state[
        "cycle_id"
    ]

    old_ath = state[
        "old_ath"
    ]

    first_entry = state[
        "first_actual_entry_session"
    ]

    fixed_exit = state[
        "fixed_exit_session"
    ]

    old_ath_recovered = int(
        state[
            "old_ath_recovered"
        ]
    )

    if old_ath_recovered not in {
        0,
        1,
    }:
        raise RecoveryStateError(
            "old_ath_recovered must be boolean-like"
        )

    if phase == "NORMAL":

        if (
            cycle_id is not None
            or
            old_ath is not None
        ):
            raise RecoveryStateError(
                "NORMAL must clear active cycle identity"
            )

        if (
            target != 0.0
            or
            first_entry is not None
            or
            fixed_exit is not None
        ):
            raise RecoveryStateError(
                "NORMAL must clear recovery exposure/clock"
            )

        if old_ath_recovered != 0:
            raise RecoveryStateError(
                "NORMAL must reset old_ath_recovered"
            )

    elif phase == "WAIT_D40":

        if (
            not cycle_id
            or
            old_ath is None
            or
            float(old_ath) <= 0.0
        ):
            raise RecoveryStateError(
                "WAIT_D40 requires cycle_id and positive old_ath"
            )

        if target != 0.0:
            raise RecoveryStateError(
                "WAIT_D40 must have zero active target"
            )

        if (
            first_entry is not None
            or
            fixed_exit is not None
        ):
            raise RecoveryStateError(
                "WAIT_D40 cannot have an H378 clock yet"
            )

    elif phase == "RECOVERY_ACTIVE":

        if (
            not cycle_id
            or
            old_ath is None
            or
            float(old_ath) <= 0.0
        ):
            raise RecoveryStateError(
                "RECOVERY_ACTIVE requires cycle_id and positive old_ath"
            )

        if target <= 0.0:
            raise RecoveryStateError(
                "RECOVERY_ACTIVE requires positive frozen target"
            )

        if (
            not first_entry
            or
            not fixed_exit
        ):
            raise RecoveryStateError(
                "RECOVERY_ACTIVE requires first entry and fixed exit"
            )

    elif phase == "OLD_ATH_GUARD":

        if (
            not cycle_id
            or
            old_ath is None
            or
            float(old_ath) <= 0.0
        ):
            raise RecoveryStateError(
                "OLD_ATH_GUARD requires cycle_id and positive old_ath"
            )

        if target != 0.0:
            raise RecoveryStateError(
                "OLD_ATH_GUARD must have zero recovery target"
            )

        if (
            not first_entry
            or
            not fixed_exit
        ):
            raise RecoveryStateError(
                "OLD_ATH_GUARD must retain H378 provenance"
            )


def validate_recovery_state(
    con: sqlite3.Connection,
) -> dict[str, Any]:
    state = load_recovery_state(
        con
    )

    _validate_state_dict(
        state
    )

    return state


def transition_recovery_state(
    con: sqlite3.Connection,
    *,
    event_key: str,
    to_phase: str,
    reason: str,
    updates: dict[str, Any] | None = None,
    payload: Any = None,
) -> RecoveryTransitionResult:
    if not event_key:
        raise RecoveryStateError(
            "event_key is required"
        )

    if not reason:
        raise RecoveryStateError(
            "reason is required"
        )

    if to_phase not in RECOVERY_PHASES:
        raise RecoveryStateError(
            f"invalid to_phase: {to_phase}"
        )

    updates = dict(
        updates or {}
    )

    allowed_update_fields = {
        "cycle_id",
        "old_ath",
        "current_target",
        "first_actual_entry_session",
        "fixed_exit_session",
        "old_ath_recovered",
    }

    unknown = (
        set(updates)
        -
        allowed_update_fields
    )

    if unknown:
        raise RecoveryStateError(
            "unsupported recovery-state updates: "
            f"{sorted(unknown)}"
        )

    updates_text = canonical_json(
        updates
    )

    payload_text = (
        None
        if payload is None
        else canonical_json(payload)
    )

    con.execute(
        "BEGIN IMMEDIATE"
    )

    try:
        existing = _fetchone_dict(
            con,
            """
            SELECT *
            FROM recovery_transitions_v01
            WHERE event_key=?
            """,
            (
                event_key,
            ),
        )

        if existing is not None:

            if (
                existing[
                    "to_phase"
                ]
                !=
                to_phase
                or
                existing[
                    "reason"
                ]
                !=
                reason
                or
                existing[
                    "updates_json"
                ]
                !=
                updates_text
                or
                existing[
                    "payload"
                ]
                !=
                payload_text
            ):
                raise RecoveryStateError(
                    "conflicting replay for transition "
                    f"{event_key}"
                )

            con.commit()

            return RecoveryTransitionResult(
                event_key=event_key,
                status="ALREADY_APPLIED",
                from_phase=str(
                    existing[
                        "from_phase"
                    ]
                ),
                to_phase=str(
                    existing[
                        "to_phase"
                    ]
                ),
                revision_before=int(
                    existing[
                        "revision_before"
                    ]
                ),
                revision_after=int(
                    existing[
                        "revision_after"
                    ]
                ),
            )

        state = load_recovery_state(
            con
        )

        from_phase = str(
            state["phase"]
        )

        allowed = (
            ALLOWED_PHASE_TRANSITIONS
            .get(
                from_phase,
                set(),
            )
        )

        if to_phase not in allowed:
            raise RecoveryStateError(
                "illegal recovery transition: "
                f"{from_phase}->{to_phase}"
            )

        target = dict(
            state
        )

        target[
            "phase"
        ] = to_phase

        target.update(
            updates
        )

        if (
            from_phase
            ==
            "RECOVERY_ACTIVE"
            and
            to_phase
            ==
            "RECOVERY_ACTIVE"
        ):
            old_target = float(
                state[
                    "current_target"
                ]
            )

            new_target = float(
                target[
                    "current_target"
                ]
            )

            if (
                new_target
                +
                TOL
                <
                old_target
            ):
                raise RecoveryStateError(
                    "recovery scale-down before H378 is forbidden"
                )

            for field in (
                "first_actual_entry_session",
                "fixed_exit_session",
            ):
                if (
                    target[field]
                    !=
                    state[field]
                ):
                    raise RecoveryStateError(
                        "later scale-up cannot change "
                        f"frozen hold clock: {field}"
                    )

        _validate_state_dict(
            target
        )

        revision_before = int(
            state[
                "revision"
            ]
        )

        revision_after = (
            revision_before
            +
            1
        )

        now = now_utc_iso()

        assignments = [
            "phase=?",
            "revision=?",
            "updated_at=?",
        ]

        values: list[Any] = [
            to_phase,
            revision_after,
            now,
        ]

        for key, value in updates.items():
            assignments.append(
                f"{key}=?"
            )
            values.append(
                value
            )

        values.append(
            1
        )

        con.execute(
            "UPDATE recovery_state_v01 SET "
            +
            ",".join(assignments)
            +
            " WHERE id=?",
            tuple(values),
        )

        con.execute(
            """
            INSERT INTO recovery_transitions_v01(
                event_key,
                revision_before,
                revision_after,
                from_phase,
                to_phase,
                reason,
                updates_json,
                payload,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                event_key,
                revision_before,
                revision_after,
                from_phase,
                to_phase,
                reason,
                updates_text,
                payload_text,
                now,
            ),
        )

        validate_recovery_state(
            con
        )

        con.commit()

        return RecoveryTransitionResult(
            event_key=event_key,
            status="APPLIED",
            from_phase=from_phase,
            to_phase=to_phase,
            revision_before=revision_before,
            revision_after=revision_after,
        )

    except Exception:
        con.rollback()
        raise


def enqueue_delayed_event(
    con: sqlite3.Connection,
    *,
    source_event_key: str,
    cycle_id: str,
    source_execution_session: str,
    maturity_session: str,
    target: float,
    source_signal_session: str | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    if (
        not source_event_key
        or
        not cycle_id
    ):
        raise RecoveryStateError(
            "source_event_key and cycle_id are required"
        )

    if (
        not source_execution_session
        or
        not maturity_session
    ):
        raise RecoveryStateError(
            "source and maturity sessions are required"
        )

    target_value = validate_frozen_target(
        target
    )

    if target_value <= 0.0:
        raise RecoveryStateError(
            "delayed event target must be positive"
        )

    payload_text = (
        None
        if payload is None
        else canonical_json(payload)
    )

    con.execute(
        "BEGIN IMMEDIATE"
    )

    try:
        existing = _fetchone_dict(
            con,
            """
            SELECT *
            FROM recovery_pending_events_v01
            WHERE source_event_key=?
            """,
            (
                source_event_key,
            ),
        )

        if existing is not None:

            checks = {
                "cycle_id":
                    cycle_id,

                "source_signal_session":
                    source_signal_session,

                "source_execution_session":
                    source_execution_session,

                "maturity_session":
                    maturity_session,

                "target":
                    target_value,

                "payload":
                    payload_text,
            }

            for key, expected in checks.items():

                actual = existing[
                    key
                ]

                if key == "target":

                    if (
                        abs(
                            float(actual)
                            -
                            float(expected)
                        )
                        >
                        TOL
                    ):
                        raise RecoveryStateError(
                            "conflicting replay for delayed event "
                            f"{source_event_key}"
                        )

                elif actual != expected:
                    raise RecoveryStateError(
                        "conflicting replay for delayed event "
                        f"{source_event_key}"
                    )

            con.commit()

            return existing

        now = now_utc_iso()

        con.execute(
            """
            INSERT INTO recovery_pending_events_v01(
                source_event_key,
                cycle_id,
                source_signal_session,
                source_execution_session,
                maturity_session,
                target,
                status,
                payload,
                created_at,
                updated_at
            )
            VALUES(
                ?,?,?,?,?,?,
                'PENDING',
                ?,?,?
            )
            """,
            (
                source_event_key,
                cycle_id,
                source_signal_session,
                source_execution_session,
                maturity_session,
                target_value,
                payload_text,
                now,
                now,
            ),
        )

        con.commit()

    except Exception:
        con.rollback()
        raise

    row = _fetchone_dict(
        con,
        """
        SELECT *
        FROM recovery_pending_events_v01
        WHERE source_event_key=?
        """,
        (
            source_event_key,
        ),
    )

    assert row is not None

    return row


def set_delayed_event_status(
    con: sqlite3.Connection,
    *,
    source_event_key: str,
    to_status: str,
) -> dict[str, Any]:
    if (
        to_status
        not in
        PENDING_EVENT_STATUSES
    ):
        raise RecoveryStateError(
            "invalid delayed-event status: "
            f"{to_status}"
        )

    con.execute(
        "BEGIN IMMEDIATE"
    )

    try:
        row = _fetchone_dict(
            con,
            """
            SELECT *
            FROM recovery_pending_events_v01
            WHERE source_event_key=?
            """,
            (
                source_event_key,
            ),
        )

        if row is None:
            raise RecoveryStateError(
                "unknown delayed event: "
                f"{source_event_key}"
            )

        current = str(
            row["status"]
        )

        if current == to_status:
            con.commit()
            return row

        if (
            to_status
            not in
            ALLOWED_EVENT_STATUS_TRANSITIONS[
                current
            ]
        ):
            raise RecoveryStateError(
                "illegal delayed-event status transition: "
                f"{current}->{to_status}"
            )

        now = now_utc_iso()

        con.execute(
            """
            UPDATE recovery_pending_events_v01
            SET
                status=?,
                updated_at=?
            WHERE source_event_key=?
            """,
            (
                to_status,
                now,
                source_event_key,
            ),
        )

        con.commit()

    except Exception:
        con.rollback()
        raise

    updated = _fetchone_dict(
        con,
        """
        SELECT *
        FROM recovery_pending_events_v01
        WHERE source_event_key=?
        """,
        (
            source_event_key,
        ),
    )

    assert updated is not None

    return updated


def pending_events(
    con: sqlite3.Connection,
    *,
    cycle_id: str | None = None,
) -> list[dict[str, Any]]:
    old_factory = con.row_factory
    con.row_factory = sqlite3.Row

    try:
        if cycle_id is None:

            rows = con.execute(
                """
                SELECT *
                FROM recovery_pending_events_v01
                WHERE status IN (
                    'PENDING',
                    'MATURED'
                )
                ORDER BY
                    maturity_session,
                    id
                """
            ).fetchall()

        else:

            rows = con.execute(
                """
                SELECT *
                FROM recovery_pending_events_v01
                WHERE
                    cycle_id=?
                    AND
                    status IN (
                        'PENDING',
                        'MATURED'
                    )
                ORDER BY
                    maturity_session,
                    id
                """,
                (
                    cycle_id,
                ),
            ).fetchall()

    finally:
        con.row_factory = old_factory

    return [
        dict(row)
        for row in rows
    ]


def apply_reserve_ledger_event(
    con: sqlite3.Connection,
    *,
    event_key: str,
    amount_eur: float,
    reason: str,
    payload: Any = None,
) -> ReserveLedgerResult:
    if (
        not event_key
        or
        not reason
    ):
        raise RecoveryStateError(
            "reserve event_key and reason are required"
        )

    amount = float(
        amount_eur
    )

    if abs(amount) <= TOL:
        raise RecoveryStateError(
            "zero-value reserve events are forbidden"
        )

    payload_text = (
        None
        if payload is None
        else canonical_json(payload)
    )

    con.execute(
        "BEGIN IMMEDIATE"
    )

    try:
        existing = _fetchone_dict(
            con,
            """
            SELECT *
            FROM recovery_reserve_ledger_v01
            WHERE event_key=?
            """,
            (
                event_key,
            ),
        )

        if existing is not None:

            if (
                abs(
                    float(
                        existing[
                            "amount_eur"
                        ]
                    )
                    -
                    amount
                )
                >
                TOL
                or
                existing[
                    "reason"
                ]
                !=
                reason
                or
                existing[
                    "payload"
                ]
                !=
                payload_text
            ):
                raise RecoveryStateError(
                    "conflicting reserve replay for "
                    f"{event_key}"
                )

            con.commit()

            return ReserveLedgerResult(
                event_key=event_key,
                status="ALREADY_APPLIED",
                old_balance_eur=float(
                    existing[
                        "balance_before_eur"
                    ]
                ),
                new_balance_eur=float(
                    existing[
                        "balance_after_eur"
                    ]
                ),
                revision_before=int(
                    existing[
                        "revision_before"
                    ]
                ),
                revision_after=int(
                    existing[
                        "revision_after"
                    ]
                ),
            )

        state = load_recovery_state(
            con
        )

        old_balance = float(
            state[
                "reserve_bucket_eur"
            ]
        )

        new_balance = (
            old_balance
            +
            amount
        )

        if new_balance < -TOL:
            raise RecoveryStateError(
                "reserve overdraw forbidden: "
                f"{old_balance:.2f} + {amount:.2f}"
            )

        new_balance = max(
            0.0,
            new_balance,
        )

        revision_before = int(
            state[
                "revision"
            ]
        )

        revision_after = (
            revision_before
            +
            1
        )

        now = now_utc_iso()

        con.execute(
            """
            UPDATE recovery_state_v01
            SET
                reserve_bucket_eur=?,
                revision=?,
                updated_at=?
            WHERE id=1
            """,
            (
                new_balance,
                revision_after,
                now,
            ),
        )

        con.execute(
            """
            INSERT INTO recovery_reserve_ledger_v01(
                event_key,
                amount_eur,
                reason,
                balance_before_eur,
                balance_after_eur,
                revision_before,
                revision_after,
                payload,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                event_key,
                amount,
                reason,
                old_balance,
                new_balance,
                revision_before,
                revision_after,
                payload_text,
                now,
            ),
        )

        validate_recovery_state(
            con
        )

        con.commit()

        return ReserveLedgerResult(
            event_key=event_key,
            status="APPLIED",
            old_balance_eur=old_balance,
            new_balance_eur=new_balance,
            revision_before=revision_before,
            revision_after=revision_after,
        )

    except Exception:
        con.rollback()
        raise
