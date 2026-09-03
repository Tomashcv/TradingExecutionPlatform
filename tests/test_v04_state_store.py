from __future__ import annotations

import json
import sqlite3

import pytest

from sp1execution.state.v04_store import (
    backup_database,
    connect,
    inspect_database,
    migrate_database,
    validate_machine_state,
)


def _legacy_db(path):
    con = sqlite3.connect(path)

    con.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE decisions (
            decision_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL
        );

        CREATE TABLE kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE order_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            quantity REAL NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            broker_order_id TEXT,
            response TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(decision_id)
                REFERENCES decisions(decision_id)
        );
        """
    )

    con.execute(
        """
        INSERT INTO decisions(
            decision_id,created_at,status,payload
        )
        VALUES('d1','2026-08-13T00:00:00+00:00','FILLED','{}')
        """
    )

    con.execute(
        """
        INSERT INTO order_attempts(
            decision_id,ticker,quantity,side,status,
            broker_order_id,response,created_at
        )
        VALUES(
            'd1','AAPL_US_EQ',50.0,'BUY','BROKER_ACCEPTED',
            '90000000001','{}','2026-08-13T00:00:00+00:00'
        )
        """
    )

    values = {
        "active_membership": {
            "month": "2026-07",
            "symbols": ["AAPL", "NVDA"],
        },
        "active_overlay": 0.0,
        "sp2_mix": {
            "AAPL": 0.5,
            "NVDA": 0.5,
        },
        "entry_policy": "IMMEDIATE_SP2",
        "entry_state": "ENTRY_COMPLETE",
        "strategy_state": "NORMAL",
        "execution_state": "IDLE",
        "capital_basis_eur": 10000.0,
        "strategy_cash_eur": 0.00,
        "external_cash_debt_eur": 0.00,
        "bootstrap_broker_debit_eur": 10000.00,
        "bootstrap_fees_eur": 10.00,
    }

    for key, value in values.items():
        con.execute(
            """
            INSERT INTO kv(key,value,updated_at)
            VALUES(?,?,?)
            """,
            (
                key,
                json.dumps(
                    value,
                    separators=(",", ":"),
                ),
                "2026-08-13T00:00:00+00:00",
            ),
        )

    con.commit()
    con.close()


def test_migration_preserves_legacy_rows_and_maps_current_state(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_db(db)

    result = migrate_database(db)

    assert result["legacy_counts_before"] == result["legacy_counts_after"]

    state = result["machine_state"]

    assert state["schema_version"] == "0.4.0"
    assert state["revision"] == 1
    assert state["lifecycle_state"] == "DEMO"
    assert state["entry_policy"] == "IMMEDIATE_SP2"
    assert state["entry_state"] == "ENTRY_COMPLETE"
    assert state["strategy_state"] == "NORMAL"
    assert state["membership_state"] == "ACTIVE"
    assert state["execution_state"] == "IDLE"

    membership = json.loads(
        state["active_membership_json"]
    )
    assert membership["symbols"] == ["AAPL", "NVDA"]

    assert state["active_overlay"] == 0.0
    assert state["capital_basis_eur"] == 10000.0
    assert state["strategy_cash_eur"] == 0.00
    assert state["external_cash_debt_eur"] == 0.00
    assert state["realized_fees_eur"] == 10.00


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_db(db)

    migrate_database(db)
    migrate_database(db)

    con = connect(db)

    try:
        assert con.execute(
            "SELECT COUNT(*) FROM machine_state"
        ).fetchone()[0] == 1

        assert con.execute(
            """
            SELECT COUNT(*)
            FROM state_transitions
            WHERE event_key='migration:v04:m0:machine-state'
            """
        ).fetchone()[0] == 1

        assert con.execute(
            "SELECT COUNT(*) FROM capital_ledger"
        ).fetchone()[0] == 2

        assert con.execute(
            "SELECT COUNT(*) FROM order_attempts"
        ).fetchone()[0] == 1

    finally:
        con.close()


def test_negative_strategy_cash_requires_matching_external_debt(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_db(db)

    migrate_database(db)

    con = connect(db)

    try:
        con.execute(
            """
            UPDATE machine_state
            SET external_cash_debt_eur=0.0
            WHERE id=1
            """
        )

        with pytest.raises(
            RuntimeError,
            match="negative strategy cash",
        ):
            validate_machine_state(con)

    finally:
        con.close()


def test_idle_machine_has_no_active_workflow(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_db(db)

    migrate_database(db)

    con = connect(db)

    try:
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
                'w1',
                NULL,
                'TEST',
                'ACTIVE',
                'SELL',
                1,
                '{}',
                '2026-08-13T00:00:00+00:00',
                '2026-08-13T00:00:00+00:00'
            )
            """
        )

        with pytest.raises(
            RuntimeError,
            match="IDLE execution state",
        ):
            validate_machine_state(con)

    finally:
        con.close()


def test_sqlite_backup_preserves_machine_state(tmp_path):
    db = tmp_path / "legacy.sqlite"
    backup = tmp_path / "backup.sqlite"

    _legacy_db(db)
    migrate_database(db)

    backup_database(db, backup)

    result = inspect_database(backup)

    assert result["machine_state"]["schema_version"] == "0.4.0"
    assert result["machine_state"]["entry_state"] == "ENTRY_COMPLETE"
    assert result["legacy_counts"]["order_attempts"] == 1
