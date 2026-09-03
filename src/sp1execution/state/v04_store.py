from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.4.0"
MIGRATION_ID = "v0.3.3q1_to_v0.4.0_m0"

DEFAULT_SOURCE_CHECKPOINT = "0a4098e"
DEFAULT_BASELINE_PROBE_SHA256 = (
    "5308a550414c24f8b04b758f36c360af2aa230b9a5a1cf1e371344128a5eb6d0"
)

LIFECYCLE_STATES = (
    "DEMO",
    "LIVE_DISARMED",
    "LIVE_ARMED",
)

ENTRY_POLICIES = (
    "UNSET",
    "IMMEDIATE_SP2",
    "WAIT_CASH",
)

ENTRY_STATES = (
    "UNINITIALIZED",
    "WAIT_CASH",
    "CRASH_BUY",
    "HANDOFF_TO_SP2",
    "ENTRY_COMPLETE",
)

STRATEGY_STATES = (
    "INACTIVE",
    "NORMAL",
    "CRASH",
    "POST_HANDOFF",
)

MEMBERSHIP_STATES = (
    "UNINITIALIZED",
    "ACTIVE",
    "MONTH_END_PENDING",
    "REBALANCE_PENDING",
)

EXECUTION_STATES = (
    "IDLE",
    "PLAN_CREATED",
    "SELL_PENDING",
    "BUY_PENDING",
    "RECONCILING",
    "PARTIAL_FILL",
    "RECONCILIATION_REQUIRED",
    "FAILED",
)

WORKFLOW_STATUSES = (
    "ACTIVE",
    "COMPLETE",
    "FAILED",
    "RECONCILIATION_REQUIRED",
)

WORKFLOW_PHASES = (
    "NONE",
    "SELL",
    "BUY",
    "RECONCILE",
)

LEG_STATUSES = (
    "PLANNED",
    "INTENT_RECORDED",
    "BROKER_ACCEPTED",
    "PENDING",
    "PARTIAL",
    "FILLED",
    "FAILED",
    "UNKNOWN",
)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table,),
    ).fetchone()
    return row is not None


def legacy_counts(con: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in ("decisions", "kv", "order_attempts"):
        if table_exists(con, table):
            result[table] = int(
                con.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            )
    return result


def get_legacy_kv(
    con: sqlite3.Connection,
    key: str,
    default: Any = None,
) -> Any:
    if not table_exists(con, "kv"):
        return default

    row = con.execute(
        "SELECT value FROM kv WHERE key=?",
        (key,),
    ).fetchone()

    if row is None:
        return default

    raw = row["value"]

    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def ensure_schema(con: sqlite3.Connection) -> None:
    lifecycle = _sql_values(LIFECYCLE_STATES)
    entry_policies = _sql_values(ENTRY_POLICIES)
    entry_states = _sql_values(ENTRY_STATES)
    strategy_states = _sql_values(STRATEGY_STATES)
    membership_states = _sql_values(MEMBERSHIP_STATES)
    execution_states = _sql_values(EXECUTION_STATES)
    workflow_statuses = _sql_values(WORKFLOW_STATUSES)
    workflow_phases = _sql_values(WORKFLOW_PHASES)
    leg_statuses = _sql_values(LEG_STATUSES)

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS v04_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS machine_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),

            schema_version TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),

            lifecycle_state TEXT NOT NULL
                CHECK (lifecycle_state IN ({lifecycle})),

            entry_policy TEXT NOT NULL
                CHECK (entry_policy IN ({entry_policies})),

            entry_state TEXT NOT NULL
                CHECK (entry_state IN ({entry_states})),

            strategy_state TEXT NOT NULL
                CHECK (strategy_state IN ({strategy_states})),

            membership_state TEXT NOT NULL
                CHECK (membership_state IN ({membership_states})),

            execution_state TEXT NOT NULL
                CHECK (execution_state IN ({execution_states})),

            active_membership_month TEXT,
            active_membership_json TEXT,

            active_overlay REAL
                CHECK (
                    active_overlay IS NULL
                    OR (
                        active_overlay >= 0.0
                        AND active_overlay <= 1.0
                    )
                ),

            sp2_mix_json TEXT,

            old_peak REAL,
            trough REAL,
            rearm_old_ath REAL,

            capital_basis_eur REAL NOT NULL
                CHECK (capital_basis_eur >= 0.0),

            strategy_cash_eur REAL NOT NULL,

            external_cash_debt_eur REAL NOT NULL
                CHECK (external_cash_debt_eur >= 0.0),

            realized_fees_eur REAL NOT NULL
                CHECK (realized_fees_eur >= 0.0),

            realized_fx_eur REAL NOT NULL,

            marked_nav_eur REAL
                CHECK (
                    marked_nav_eur IS NULL
                    OR marked_nav_eur >= 0.0
                ),

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS state_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            event_key TEXT NOT NULL UNIQUE,

            revision_before INTEGER,
            revision_after INTEGER NOT NULL,

            dimension TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,

            reason TEXT NOT NULL,
            decision_id TEXT,
            payload TEXT,

            created_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS execution_workflows (
            workflow_id TEXT PRIMARY KEY,

            decision_id TEXT,
            kind TEXT NOT NULL,

            status TEXT NOT NULL
                CHECK (status IN ({workflow_statuses})),

            phase TEXT NOT NULL
                CHECK (phase IN ({workflow_phases})),

            source_state_revision INTEGER NOT NULL,
            target_payload TEXT NOT NULL,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS execution_legs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            workflow_id TEXT NOT NULL,
            leg_index INTEGER NOT NULL CHECK (leg_index >= 0),

            side TEXT NOT NULL
                CHECK (side IN ('BUY','SELL')),

            logical_symbol TEXT NOT NULL,
            broker_ticker TEXT NOT NULL,

            intended_quantity REAL NOT NULL,
            broker_order_id TEXT,

            status TEXT NOT NULL
                CHECK (status IN ({leg_statuses})),

            payload TEXT,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            UNIQUE (workflow_id, leg_index),

            FOREIGN KEY (workflow_id)
                REFERENCES execution_workflows(workflow_id)
                ON DELETE RESTRICT
        )
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_execution_legs_broker_order
        ON execution_legs(broker_order_id)
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS capital_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            event_key TEXT NOT NULL UNIQUE,

            decision_id TEXT,
            broker_order_id TEXT,
            fill_id TEXT,

            event_type TEXT NOT NULL,

            cash_delta_eur REAL NOT NULL,
            fee_eur REAL NOT NULL DEFAULT 0.0
                CHECK (fee_eur >= 0.0),

            fx_rate REAL,

            payload TEXT,

            created_at TEXT NOT NULL
        )
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_capital_ledger_decision
        ON capital_ledger(decision_id)
        """
    )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_capital_ledger_broker_order
        ON capital_ledger(broker_order_id)
        """
    )


def _float_value(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def validate_machine_state(con: sqlite3.Connection) -> dict[str, Any]:
    row = con.execute(
        "SELECT * FROM machine_state WHERE id=1"
    ).fetchone()

    if row is None:
        raise RuntimeError("machine_state row id=1 is missing")

    state = dict(row)

    if state["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError(
            "Unexpected schema version: "
            f"{state['schema_version']} != {SCHEMA_VERSION}"
        )

    if state["lifecycle_state"] not in LIFECYCLE_STATES:
        raise RuntimeError("Invalid lifecycle_state")

    if state["entry_policy"] not in ENTRY_POLICIES:
        raise RuntimeError("Invalid entry_policy")

    if state["entry_state"] not in ENTRY_STATES:
        raise RuntimeError("Invalid entry_state")

    if state["strategy_state"] not in STRATEGY_STATES:
        raise RuntimeError("Invalid strategy_state")

    if state["membership_state"] not in MEMBERSHIP_STATES:
        raise RuntimeError("Invalid membership_state")

    if state["execution_state"] not in EXECUTION_STATES:
        raise RuntimeError("Invalid execution_state")

    membership = None
    if state["active_membership_json"] is not None:
        membership = json.loads(state["active_membership_json"])

        symbols = membership.get("symbols")
        if (
            not isinstance(symbols, list)
            or len(symbols) != 2
            or len(set(symbols)) != 2
        ):
            raise RuntimeError(
                "active membership must contain exactly two distinct symbols"
            )

    if state["entry_state"] == "ENTRY_COMPLETE":
        if membership is None:
            raise RuntimeError(
                "ENTRY_COMPLETE requires active membership"
            )
        if state["membership_state"] == "UNINITIALIZED":
            raise RuntimeError(
                "ENTRY_COMPLETE cannot have uninitialized membership"
            )
        if state["strategy_state"] == "INACTIVE":
            raise RuntimeError(
                "ENTRY_COMPLETE cannot have inactive strategy"
            )

    overlay = state["active_overlay"]
    if overlay is not None and not 0.0 <= float(overlay) <= 1.0:
        raise RuntimeError("active_overlay outside [0,1]")

    if state["sp2_mix_json"] is not None:
        mix = json.loads(state["sp2_mix_json"])
        total = sum(float(v) for v in mix.values())
        if abs(total - 1.0) > 1e-8:
            raise RuntimeError(
                f"sp2_mix must sum to 1.0, got {total}"
            )

    strategy_cash = float(state["strategy_cash_eur"])
    debt = float(state["external_cash_debt_eur"])

    if strategy_cash < -0.01 and debt + strategy_cash < -0.011:
        raise RuntimeError(
            "negative strategy cash is not fully represented by "
            "external_cash_debt"
        )

    active_workflows = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM execution_workflows
            WHERE status='ACTIVE'
            """
        ).fetchone()[0]
    )

    if state["execution_state"] == "IDLE" and active_workflows != 0:
        raise RuntimeError(
            "IDLE execution state cannot have an ACTIVE workflow"
        )

    return state


def _set_meta(
    con: sqlite3.Connection,
    key: str,
    value: Any,
) -> None:
    now = now_utc_iso()
    con.execute(
        """
        INSERT INTO v04_meta(key,value,updated_at)
        VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (
            key,
            canonical_json(value),
            now,
        ),
    )


def migrate_database(
    db_path: str | Path,
    *,
    source_checkpoint: str = DEFAULT_SOURCE_CHECKPOINT,
    baseline_probe_sha256: str = DEFAULT_BASELINE_PROBE_SHA256,
    lifecycle_state: str = "DEMO",
) -> dict[str, Any]:
    if lifecycle_state not in LIFECYCLE_STATES:
        raise ValueError(
            f"Invalid lifecycle_state: {lifecycle_state}"
        )

    db_path = Path(db_path)

    if not db_path.exists():
        raise FileNotFoundError(db_path)

    con = connect(db_path)

    try:
        before = legacy_counts(con)

        con.execute("BEGIN IMMEDIATE")

        try:
            ensure_schema(con)

            machine = con.execute(
                "SELECT * FROM machine_state WHERE id=1"
            ).fetchone()

            if machine is None:
                active_membership = get_legacy_kv(
                    con,
                    "active_membership",
                    None,
                )
                active_overlay = get_legacy_kv(
                    con,
                    "active_overlay",
                    None,
                )
                sp2_mix = get_legacy_kv(
                    con,
                    "sp2_mix",
                    None,
                )

                entry_policy = str(
                    get_legacy_kv(
                        con,
                        "entry_policy",
                        "UNSET",
                    )
                )
                entry_state = str(
                    get_legacy_kv(
                        con,
                        "entry_state",
                        "UNINITIALIZED",
                    )
                )
                strategy_state = str(
                    get_legacy_kv(
                        con,
                        "strategy_state",
                        "INACTIVE",
                    )
                )
                execution_state = str(
                    get_legacy_kv(
                        con,
                        "execution_state",
                        "IDLE",
                    )
                )

                membership_state = (
                    "ACTIVE"
                    if active_membership is not None
                    else "UNINITIALIZED"
                )

                capital_basis = _float_value(
                    get_legacy_kv(
                        con,
                        "capital_basis_eur",
                        0.0,
                    )
                )
                strategy_cash = _float_value(
                    get_legacy_kv(
                        con,
                        "strategy_cash_eur",
                        0.0,
                    )
                )
                external_debt = _float_value(
                    get_legacy_kv(
                        con,
                        "external_cash_debt_eur",
                        0.0,
                    )
                )
                bootstrap_fees = _float_value(
                    get_legacy_kv(
                        con,
                        "bootstrap_fees_eur",
                        0.0,
                    )
                )
                bootstrap_debit = _float_value(
                    get_legacy_kv(
                        con,
                        "bootstrap_broker_debit_eur",
                        0.0,
                    )
                )

                membership_month = None
                if isinstance(active_membership, dict):
                    membership_month = active_membership.get("month")

                now = now_utc_iso()

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
                        1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        SCHEMA_VERSION,
                        1,
                        lifecycle_state,
                        entry_policy,
                        entry_state,
                        strategy_state,
                        membership_state,
                        execution_state,
                        membership_month,
                        (
                            None
                            if active_membership is None
                            else canonical_json(active_membership)
                        ),
                        (
                            None
                            if active_overlay is None
                            else float(active_overlay)
                        ),
                        (
                            None
                            if sp2_mix is None
                            else canonical_json(sp2_mix)
                        ),
                        None,
                        None,
                        None,
                        capital_basis,
                        strategy_cash,
                        external_debt,
                        bootstrap_fees,
                        0.0,
                        None,
                        now,
                        now,
                    ),
                )

                con.execute(
                    """
                    INSERT OR IGNORE INTO state_transitions(
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
                        "migration:v04:m0:machine-state",
                        None,
                        1,
                        "MIGRATION",
                        None,
                        "V0.4_INITIALIZED",
                        MIGRATION_ID,
                        None,
                        canonical_json(
                            {
                                "source_checkpoint": source_checkpoint,
                                "baseline_probe_sha256": (
                                    baseline_probe_sha256
                                ),
                            }
                        ),
                        now,
                    ),
                )

                if capital_basis > 0.0:
                    con.execute(
                        """
                        INSERT OR IGNORE INTO capital_ledger(
                            event_key,
                            event_type,
                            cash_delta_eur,
                            fee_eur,
                            payload,
                            created_at
                        )
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            "migration:v04:m0:capital-basis",
                            "CAPITAL_BASIS",
                            capital_basis,
                            0.0,
                            canonical_json(
                                {
                                    "source": "legacy_kv",
                                }
                            ),
                            now,
                        ),
                    )

                if bootstrap_debit > 0.0:
                    con.execute(
                        """
                        INSERT OR IGNORE INTO capital_ledger(
                            event_key,
                            event_type,
                            cash_delta_eur,
                            fee_eur,
                            payload,
                            created_at
                        )
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            "migration:v04:m0:bootstrap-debit",
                            "BOOTSTRAP_BROKER_DEBIT",
                            -bootstrap_debit,
                            bootstrap_fees,
                            canonical_json(
                                {
                                    "source": "legacy_kv",
                                    "includes_broker_wallet_impact": True,
                                }
                            ),
                            now,
                        ),
                    )

            else:
                if machine["schema_version"] != SCHEMA_VERSION:
                    raise RuntimeError(
                        "Existing machine_state has incompatible "
                        f"schema {machine['schema_version']}"
                    )

            _set_meta(
                con,
                "schema_version",
                SCHEMA_VERSION,
            )
            _set_meta(
                con,
                "migration_id",
                MIGRATION_ID,
            )
            _set_meta(
                con,
                "source_checkpoint",
                source_checkpoint,
            )
            _set_meta(
                con,
                "baseline_probe_sha256",
                baseline_probe_sha256,
            )

            state = validate_machine_state(con)

            after = legacy_counts(con)

            if before != after:
                raise RuntimeError(
                    "Legacy table row counts changed during additive migration: "
                    f"before={before} after={after}"
                )

            con.commit()

        except Exception:
            con.rollback()
            raise

        return {
            "schema_version": SCHEMA_VERSION,
            "migration_id": MIGRATION_ID,
            "legacy_counts_before": before,
            "legacy_counts_after": after,
            "machine_state": state,
        }

    finally:
        con.close()


def backup_database(
    source: str | Path,
    destination: str | Path,
) -> None:
    source = Path(source)
    destination = Path(destination)

    if not source.exists():
        raise FileNotFoundError(source)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(destination))

    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def inspect_database(
    db_path: str | Path,
) -> dict[str, Any]:
    con = connect(db_path)

    try:
        state = validate_machine_state(con)

        workflow_count = int(
            con.execute(
                "SELECT COUNT(*) FROM execution_workflows"
            ).fetchone()[0]
        )

        leg_count = int(
            con.execute(
                "SELECT COUNT(*) FROM execution_legs"
            ).fetchone()[0]
        )

        transition_count = int(
            con.execute(
                "SELECT COUNT(*) FROM state_transitions"
            ).fetchone()[0]
        )

        capital_ledger_count = int(
            con.execute(
                "SELECT COUNT(*) FROM capital_ledger"
            ).fetchone()[0]
        )

        return {
            "machine_state": state,
            "legacy_counts": legacy_counts(con),
            "workflow_count": workflow_count,
            "leg_count": leg_count,
            "transition_count": transition_count,
            "capital_ledger_count": capital_ledger_count,
        }

    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sp1execution.state.v04_store"
    )
    sub = parser.add_subparsers(required=True)

    migrate = sub.add_parser("migrate")
    migrate.add_argument("--db", required=True)
    migrate.add_argument(
        "--source-checkpoint",
        default=DEFAULT_SOURCE_CHECKPOINT,
    )
    migrate.add_argument(
        "--baseline-probe-sha256",
        default=DEFAULT_BASELINE_PROBE_SHA256,
    )
    migrate.add_argument(
        "--lifecycle",
        default="DEMO",
        choices=LIFECYCLE_STATES,
    )

    validate = sub.add_parser("validate")
    validate.add_argument("--db", required=True)

    inspect_cmd = sub.add_parser("inspect")
    inspect_cmd.add_argument("--db", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("--db", required=True)
    backup.add_argument("--out", required=True)

    args = parser.parse_args()

    if args.__dict__.get("db") is not None:
        db_path = Path(args.db)
    else:
        db_path = None

    if args.__dict__.get("out") is not None:
        out_path = Path(args.out)
    else:
        out_path = None

    if args.__dict__.get("lifecycle") is not None:
        lifecycle = args.lifecycle
    else:
        lifecycle = "DEMO"

    if args.__dict__.get("source_checkpoint") is not None:
        source_checkpoint = args.source_checkpoint
    else:
        source_checkpoint = DEFAULT_SOURCE_CHECKPOINT

    if args.__dict__.get("baseline_probe_sha256") is not None:
        baseline_probe_sha256 = args.baseline_probe_sha256
    else:
        baseline_probe_sha256 = DEFAULT_BASELINE_PROBE_SHA256

    command = sys_argv_command()

    if command == "migrate":
        result = migrate_database(
            db_path,
            source_checkpoint=source_checkpoint,
            baseline_probe_sha256=baseline_probe_sha256,
            lifecycle_state=lifecycle,
        )

        print("M0_STATE_SCHEMA=PASS")
        print("MIGRATION_CURRENT_STATE=PASS")
        print("LEGACY_COUNTS_UNCHANGED=PASS")
        print(
            "ORDER_ATTEMPTS_CREATED="
            + str(
                result["legacy_counts_after"].get(
                    "order_attempts",
                    0,
                )
                - result["legacy_counts_before"].get(
                    "order_attempts",
                    0,
                )
            )
        )

        state = result["machine_state"]

        print(f"SCHEMA_VERSION={state['schema_version']}")
        print(f"STATE_REVISION={state['revision']}")
        print(f"LIFECYCLE_STATE={state['lifecycle_state']}")
        print(f"ENTRY_POLICY={state['entry_policy']}")
        print(f"ENTRY_STATE={state['entry_state']}")
        print(f"STRATEGY_STATE={state['strategy_state']}")
        print(f"MEMBERSHIP_STATE={state['membership_state']}")
        print(f"EXECUTION_STATE={state['execution_state']}")
        print(
            "ACTIVE_MEMBERSHIP="
            + str(state["active_membership_json"])
        )
        print(
            f"ACTIVE_OVERLAY={state['active_overlay']}"
        )
        print(
            f"CAPITAL_BASIS_EUR="
            f"{float(state['capital_basis_eur']):.2f}"
        )
        print(
            f"STRATEGY_CASH_EUR="
            f"{float(state['strategy_cash_eur']):.2f}"
        )
        print(
            f"EXTERNAL_CASH_DEBT_EUR="
            f"{float(state['external_cash_debt_eur']):.2f}"
        )
        print(
            f"REALIZED_FEES_EUR="
            f"{float(state['realized_fees_eur']):.2f}"
        )

        return 0

    if command == "validate":
        con = connect(db_path)
        try:
            validate_machine_state(con)
        finally:
            con.close()

        print("V04_STATE_VALIDATION=PASS")
        return 0

    if command == "inspect":
        result = inspect_database(db_path)
        print(
            json.dumps(
                result,
                sort_keys=True,
                indent=2,
            )
        )
        return 0

    if command == "backup":
        backup_database(
            db_path,
            out_path,
        )
        print(f"BACKUP_CREATED={out_path}")
        return 0

    raise RuntimeError(f"Unknown command: {command}")


def sys_argv_command() -> str:
    import sys

    if len(sys.argv) < 2:
        raise RuntimeError("Missing command")
    return sys.argv[1]


if __name__ == "__main__":
    raise SystemExit(main())
