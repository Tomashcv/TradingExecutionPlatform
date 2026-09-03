from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sp1execution.engine.planner import PlannedOrder
from sp1execution.execution.broker_executor_v04 import (
    PhaseNotReady,
    create_and_start_from_fresh_orders,
    submit_current_phase,
    workflow_id_for_decision,
)
from sp1execution.execution.recovery_v04 import classify_recovery
from sp1execution.state.v04_store import connect, ensure_schema

NOW = "2026-08-14T13:40:00+00:00"


class BlockingBroker:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(t212_env="demo")
        self.entered_post = threading.Event()
        self.release_post = threading.Event()
        self._lock = threading.Lock()
        self.submissions: list[tuple[str, float]] = []
        self.next_id = 1000

    def pending_orders(self):
        return []

    def market_order_demo_only(self, ticker, quantity):
        with self._lock:
            self.submissions.append((str(ticker), float(quantity)))
            self.next_id += 1
            oid = str(self.next_id)

        self.entered_post.set()
        if not self.release_post.wait(timeout=10.0):
            raise RuntimeError("test broker release timeout")

        return {
            "id": oid,
            "ticker": ticker,
            "quantity": quantity,
            "status": "NEW",
        }


def _init_file_db(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
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
                1,'0.4.0',1,'DEMO','IMMEDIATE_SP2','ENTRY_COMPLETE',
                'NORMAL','ACTIVE','IDLE','2026-07',
                '{"month":"2026-07","symbols":["AAPL","NVDA"]}',
                0.0,'{"AAPL":0.5,"NVDA":0.5}',
                NULL,NULL,NULL,
                10000.0,1000.0,0.0,10.00,0.0,NULL,?,?
            )
            """,
            (NOW, NOW),
        )
        con.commit()

        decision_id = "m9a-concurrent-submit"
        create_and_start_from_fresh_orders(
            con,
            decision_id=decision_id,
            decision_payload={
                "decision_id": decision_id,
                "target_weights": {
                    "NVDA": 0.0,
                    "VUAA": 1.0,
                },
                "strategy_broker_tickers": [
                    "AAPL_US_EQ",
                    "NVDA_US_EQ",
                    "VUAAm_EQ",
                ],
            },
            positions=[
                {
                    "instrument": {"ticker": "NVDA_US_EQ"},
                    "quantity": 10.0,
                }
            ],
            orders=[
                PlannedOrder(
                    logical_symbol="NVDA",
                    broker_ticker="NVDA_US_EQ",
                    quantity=-10.0,
                    side="SELL",
                    estimated_notional_eur=1000.0,
                    delta_eur=-1000.0,
                )
            ],
            created_at=NOW,
        )
        return workflow_id_for_decision(decision_id)
    finally:
        con.close()


def test_concurrent_submitters_cannot_duplicate_same_broker_post(tmp_path):
    db_path = tmp_path / "race.sqlite"
    workflow_id = _init_file_db(db_path)
    broker = BlockingBroker()

    first_result = {}
    first_error = {}

    def first_worker():
        con = connect(db_path)
        try:
            try:
                first_result["result"] = submit_current_phase(
                    con,
                    workflow_id=workflow_id,
                    broker=broker,
                    now_fn=lambda: NOW,
                )
            except (RuntimeError, sqlite3.Error) as exc:
                first_error["error"] = exc
        finally:
            con.close()

    thread = threading.Thread(target=first_worker, daemon=True)
    thread.start()

    assert broker.entered_post.wait(timeout=10.0)
    assert broker.submissions == [("NVDA_US_EQ", -10.0)]

    second_con = connect(db_path)
    try:
        with pytest.raises(PhaseNotReady, match="submission lock"):
            submit_current_phase(
                second_con,
                workflow_id=workflow_id,
                broker=broker,
                now_fn=lambda: NOW,
            )
    finally:
        second_con.close()

    assert broker.submissions == [("NVDA_US_EQ", -10.0)]

    broker.release_post.set()
    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert first_error == {}

    result = first_result["result"]
    assert result.phase == "SELL"
    assert len(result.broker_order_ids) == 1
    assert broker.submissions == [("NVDA_US_EQ", -10.0)]

    con = connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        leg = dict(
            con.execute(
                """
                SELECT *
                FROM execution_legs
                WHERE workflow_id=? AND leg_index=0
                """,
                (workflow_id,),
            ).fetchone()
        )
        assert leg["status"] == "BROKER_ACCEPTED"
        assert leg["broker_order_id"] == result.broker_order_ids[0]

        recovery = classify_recovery(
            con,
            workflow_id=workflow_id,
            created_at=NOW,
        )
        assert recovery.action == "RECONCILE_SELL"
        assert not recovery.may_submit_order
        assert recovery.requires_broker_read
    finally:
        con.close()


def test_submission_lock_is_scoped_to_same_sqlite_database(tmp_path):
    db1 = tmp_path / "one.sqlite"
    db2 = tmp_path / "two.sqlite"
    wf1 = _init_file_db(db1)
    wf2 = _init_file_db(db2)

    broker1 = BlockingBroker()
    broker2 = BlockingBroker()
    errors: list[Exception] = []

    def worker(path, workflow_id, broker):
        con = connect(path)
        try:
            submit_current_phase(
                con,
                workflow_id=workflow_id,
                broker=broker,
                now_fn=lambda: NOW,
            )
        except (RuntimeError, sqlite3.Error) as exc:
            errors.append(exc)
        finally:
            con.close()

    t1 = threading.Thread(target=worker, args=(db1, wf1, broker1), daemon=True)
    t2 = threading.Thread(target=worker, args=(db2, wf2, broker2), daemon=True)

    t1.start()
    t2.start()

    assert broker1.entered_post.wait(timeout=10.0)
    assert broker2.entered_post.wait(timeout=10.0)

    broker1.release_post.set()
    broker2.release_post.set()

    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert errors == []
    assert len(broker1.submissions) == 1
    assert len(broker2.submissions) == 1
