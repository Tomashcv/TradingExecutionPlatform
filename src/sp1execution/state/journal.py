from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = '''
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    quantity REAL NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    response TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
);
'''


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Journal:
    def __init__(self, path: Path | str = "state/sp1execution.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_kv(self, key: str):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def set_kv(self, key: str, value) -> None:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                '''
                INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                ''',
                (key, raw, utc_now()),
            )

    def put_decision(self, decision_id: str, status: str, payload: dict) -> None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO decisions(decision_id,created_at,status,payload) VALUES(?,?,?,?)",
                (decision_id, utc_now(), status, raw),
            )

    def get_decision(self, decision_id: str):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": row["decision_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "payload": json.loads(row["payload"]),
        }

    def update_decision_status(self, decision_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE decisions SET status=? WHERE decision_id=?",
                (status, decision_id),
            )

    def has_order_attempts(self, decision_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM order_attempts WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return bool(row["n"])

    def accepted_order_attempts(self, decision_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                '''
                SELECT ticker,quantity,side,broker_order_id,response,created_at
                FROM order_attempts
                WHERE decision_id=? AND status='BROKER_ACCEPTED'
                ORDER BY id
                ''',
                (decision_id,),
            ).fetchall()

        return [
            {
                "ticker": row["ticker"],
                "quantity": float(row["quantity"]),
                "side": row["side"],
                "broker_order_id": row["broker_order_id"],
                "response": None if row["response"] is None else json.loads(row["response"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_order_attempt(
        self,
        *,
        decision_id: str,
        ticker: str,
        quantity: float,
        side: str,
        status: str,
        broker_order_id: str | None = None,
        response=None,
    ) -> None:
        raw = None if response is None else json.dumps(response, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                '''
                INSERT INTO order_attempts(
                    decision_id,ticker,quantity,side,status,broker_order_id,response,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ''',
                (
                    decision_id,
                    ticker,
                    quantity,
                    side,
                    status,
                    broker_order_id,
                    raw,
                    utc_now(),
                ),
            )
