from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sp1execution.engine.planner import PlannedOrder
from sp1execution.execution.broker_executor_v04 import (
    create_and_start_from_fresh_orders,
    workflow_id_for_decision,
)
from sp1execution.execution.recovery_v04 import classify_recovery
from sp1execution.state.v04_store import connect, ensure_schema

NOW = "2026-08-14T14:00:00+00:00"

CHILD_CODE = r"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from sp1execution.execution.broker_executor_v04 import submit_current_phase
from sp1execution.state.v04_store import connect

db_path = Path(sys.argv[1])
workflow_id = sys.argv[2]
post_log = Path(sys.argv[3])
entered = Path(sys.argv[4])
release = Path(sys.argv[5])
mode = sys.argv[6]
now = sys.argv[7]


class FileBroker:
    def __init__(self):
        self.settings = SimpleNamespace(t212_env="demo")

    def pending_orders(self):
        return []

    def market_order_demo_only(self, ticker, quantity):
        with post_log.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "ticker": ticker,
                        "quantity": quantity,
                        "mode": mode,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            fh.flush()
            os.fsync(fh.fileno())

        entered.write_text(str(os.getpid()))

        if mode == "hold":
            deadline = time.time() + 20.0
            while time.time() < deadline:
                if release.exists():
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("release timeout")

        return {
            "id": f"pid-{os.getpid()}",
            "ticker": ticker,
            "quantity": quantity,
            "status": "NEW",
        }


broker = FileBroker()
con = connect(db_path)

try:
    result = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: now,
    )
except BaseException as exc:
    print(
        json.dumps(
            {
                "status": "ERROR",
                "type": type(exc).__name__,
                "message": str(exc),
            },
            sort_keys=True,
        )
    )
    raise
else:
    print(
        json.dumps(
            {
                "status": "OK",
                "phase": result.phase,
                "broker_order_ids": list(result.broker_order_ids),
            },
            sort_keys=True,
        )
    )
finally:
    con.close()
"""


def _init_db(path: Path) -> str:
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
                10000.0,1000.0,0.0,10.0,0.0,NULL,?,?
            )
            """,
            (NOW, NOW),
        )
        con.commit()

        decision_id = "m9b-process-race"
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


def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {path}")


def _post_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _child_args(
    db: Path,
    workflow_id: str,
    post_log: Path,
    entered: Path,
    release: Path,
    mode: str,
) -> list[str]:
    return [
        sys.executable,
        "-c",
        CHILD_CODE,
        str(db),
        workflow_id,
        str(post_log),
        str(entered),
        str(release),
        mode,
        NOW,
    ]


def _read_leg_state(db: Path, workflow_id: str) -> tuple[dict, dict, dict]:
    con = connect(db)
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
        state = dict(con.execute("SELECT * FROM machine_state WHERE id=1").fetchone())
        workflow = dict(
            con.execute(
                """
                SELECT *
                FROM execution_workflows
                WHERE workflow_id=?
                """,
                (workflow_id,),
            ).fetchone()
        )
        return leg, state, workflow
    finally:
        con.close()


def test_cross_process_submission_lock_allows_at_most_one_post(tmp_path):
    db = tmp_path / "exclusion.sqlite"
    workflow_id = _init_db(db)
    post_log = tmp_path / "posts.jsonl"
    entered = tmp_path / "entered"
    release = tmp_path / "release"

    p1 = subprocess.Popen(
        _child_args(
            db,
            workflow_id,
            post_log,
            entered,
            release,
            "hold",
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_file(entered)
        assert len(_post_records(post_log)) == 1

        p2 = subprocess.run(
            _child_args(
                db,
                workflow_id,
                post_log,
                tmp_path / "p2-entered",
                tmp_path / "p2-release",
                "return",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        assert p2.returncode != 0
        assert "submission lock" in p2.stdout
        assert len(_post_records(post_log)) == 1

        release.write_text("go")
        out1, _ = p1.communicate(timeout=10.0)
        assert p1.returncode == 0, out1

        leg, _, _ = _read_leg_state(db, workflow_id)
        assert leg["status"] == "BROKER_ACCEPTED"
        assert leg["broker_order_id"]

        con = connect(db)
        try:
            recovery = classify_recovery(
                con,
                workflow_id=workflow_id,
                created_at=NOW,
            )
        finally:
            con.close()

        assert recovery.action == "RECONCILE_SELL"
        assert not recovery.may_submit_order
        assert recovery.requires_broker_read
    finally:
        if p1.poll() is None:
            p1.kill()
            p1.wait(timeout=5.0)


def test_sigkill_releases_lock_but_ambiguous_intent_never_resubmits(tmp_path):
    db = tmp_path / "crash.sqlite"
    workflow_id = _init_db(db)
    post_log = tmp_path / "posts.jsonl"
    entered = tmp_path / "entered"
    release = tmp_path / "release"

    p1 = subprocess.Popen(
        _child_args(
            db,
            workflow_id,
            post_log,
            entered,
            release,
            "hold",
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_file(entered)

        leg, _, _ = _read_leg_state(db, workflow_id)
        assert leg["status"] == "INTENT_RECORDED"
        assert leg["broker_order_id"] is None
        assert len(_post_records(post_log)) == 1

        os.kill(p1.pid, signal.SIGKILL)
        p1.communicate(timeout=5.0)
        assert p1.returncode == -signal.SIGKILL

        p2 = subprocess.run(
            _child_args(
                db,
                workflow_id,
                post_log,
                tmp_path / "p2-entered",
                tmp_path / "p2-release",
                "return",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        assert p2.returncode != 0
        assert "automatic retry forbidden" in p2.stdout
        assert len(_post_records(post_log)) == 1

        con = connect(db)
        try:
            recovery = classify_recovery(
                con,
                workflow_id=workflow_id,
                created_at=NOW,
            )
        finally:
            con.close()

        assert recovery.action == "MANUAL_RECONCILIATION"
        assert not recovery.may_submit_order

        _, state, workflow = _read_leg_state(db, workflow_id)
        assert state["execution_state"] == "RECONCILIATION_REQUIRED"
        assert workflow["status"] == "RECONCILIATION_REQUIRED"
    finally:
        if p1.poll() is None:
            p1.kill()
            p1.wait(timeout=5.0)
