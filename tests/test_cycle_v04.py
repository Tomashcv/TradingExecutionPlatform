from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sp1execution.engine.planner import InstrumentQuote
from sp1execution.execution.cycle_v04 import (
    CycleSafetyError,
    MarketSnapshot,
    _apply_control_target,
    run_cycle,
)
from sp1execution.state.v04_store import ensure_schema

NY = ZoneInfo("America/New_York")
REGULAR = datetime(2026, 8, 13, 11, 0, tzinfo=NY)
CLOSED = datetime(2026, 8, 13, 23, 0, tzinfo=NY)


class FakeBroker:
    def __init__(self):
        self.settings = SimpleNamespace(t212_env="demo")
        self.posts = []
        self.pending = []
        self._positions = []
        self.available_cash = 50000.0

    def market_order_demo_only(self, ticker, quantity):
        self.posts.append((ticker, quantity))
        return {"id": f"order-{len(self.posts)}"}

    def pending_orders(self):
        return list(self.pending)

    def _request(self, method, path, body=None):
        assert method == "GET"
        return {"items": [], "nextPagePath": None}

    def positions(self, force_refresh=False):
        return list(self._positions)

    def account_summary(self, force_refresh=False):
        return {"cash": {"availableToTrade": self.available_cash}}


class FakeMarket:
    pass


def _db(
    *,
    overlay=0.0,
    strategy_state="NORMAL",
    membership_state="ACTIVE",
    cash=0.0,
    debt=0.0,
):
    con = sqlite3.connect(":memory:")
    ensure_schema(con)
    con.execute(
        """
        INSERT INTO machine_state(
            id,schema_version,revision,lifecycle_state,entry_policy,entry_state,
            strategy_state,membership_state,execution_state,
            active_membership_month,active_membership_json,active_overlay,
            sp2_mix_json,old_peak,trough,rearm_old_ath,capital_basis_eur,
            strategy_cash_eur,external_cash_debt_eur,realized_fees_eur,
            realized_fx_eur,marked_nav_eur,created_at,updated_at
        )
        VALUES(
            1,'0.4.0',1,'DEMO','IMMEDIATE_SP2','ENTRY_COMPLETE',
            ?,?,'IDLE','2026-07',
            '{"month":"2026-07","symbols":["AAPL","NVDA"]}',
            ?,'{"AAPL":0.5,"NVDA":0.5}',800.0,NULL,NULL,10000.0,
            ?,?,10.00,0.0,NULL,
            '2026-08-13T00:00:00+00:00','2026-08-13T00:00:00+00:00'
        )
        """,
        (strategy_state, membership_state, overlay, cash, debt),
    )
    con.commit()
    return con


def _membership(month="2026-07", symbols=("AAPL", "NVDA")):
    return SimpleNamespace(
        month=month,
        source_as_of=f"{month}-29",
        symbols=symbols,
        source="test",
    )


def _overlay(
    *,
    mode="NORMAL",
    target=0.0,
    as_of="2026-08-12",
    old_peak=800.0,
    trough=None,
):
    return SimpleNamespace(
        as_of=as_of,
        ivv_close=780.0,
        mode=mode,
        old_peak=old_peak,
        trough=trough,
        drawdown=-0.025,
        recovery=None,
        target_sp500=target,
        last_event="NO_ACTION",
    )


def _snapshot(broker, market, symbols):
    logicals = set(symbols) | {"VUAA"}
    ticker_map = {
        "AAPL": "AAPL_US_EQ",
        "NVDA": "NVDA_US_EQ",
        "MSFT": "MSFT_US_EQ",
        "VUAA": "VUAAm_EQ",
    }
    quotes = {
        logical: InstrumentQuote(
            logical_symbol=logical,
            broker_ticker=ticker_map[logical],
            price=100.0,
            currency="EUR" if logical == "VUAA" else "USD",
        )
        for logical in logicals
    }
    values = {logical: 0.0 for logical in logicals}
    values["AAPL"] = 5000.0
    values["NVDA"] = 5000.0
    positions = [
        {"ticker": "AAPL_US_EQ", "quantity": 50.0},
        {"ticker": "NVDA_US_EQ", "quantity": 50.0},
    ]
    broker._positions = positions
    return MarketSnapshot(
        positions=positions,
        quotes=quotes,
        current_values_eur=values,
        eurusd=1.0,
        strategy_broker_tickers=tuple(sorted(quote.broker_ticker for quote in quotes.values())),
        max_quote_age_seconds=1.0,
    )


def _noop_guard(*args, **kwargs):
    return None


def _run(
    con,
    broker,
    *,
    now,
    membership,
    overlay,
    confirm=False,
):
    return run_cycle(
        con,
        broker=broker,
        market=FakeMarket(),
        allow_demo_submit=confirm,
        now_fn=lambda: now,
        membership_loader=lambda: membership,
        overlay_loader=lambda market: overlay,
        snapshot_builder=_snapshot,
        submission_guard=_noop_guard,
    )


def test_no_trade_commits_local_snapshot_and_never_posts():
    con = _db()
    broker = FakeBroker()
    result = _run(
        con,
        broker,
        now=CLOSED,
        membership=_membership(),
        overlay=_overlay(),
    )
    assert result.action == "NO_TRADE_CONTROL_COMMITTED"
    assert broker.posts == []
    assert con.execute(
        "SELECT execution_state,strategy_state FROM machine_state WHERE id=1"
    ).fetchone() == ("IDLE", "NORMAL")


def test_trade_outside_regular_session_waits_without_workflow():
    con = _db()
    broker = FakeBroker()
    result = _run(
        con,
        broker,
        now=CLOSED,
        membership=_membership(),
        overlay=_overlay(mode="CRASH", target=0.1, trough=700.0),
    )
    assert result.action == "WAIT_REGULAR_SESSION"
    assert broker.posts == []
    assert con.execute("SELECT COUNT(*) FROM execution_workflows").fetchone()[0] == 0


def test_regular_trade_creates_workflow_but_no_same_cycle_post():
    con = _db()
    broker = FakeBroker()
    result = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=_overlay(mode="CRASH", target=0.1, trough=700.0),
        confirm=True,
    )
    assert result.action == "WORKFLOW_CREATED"
    assert broker.posts == []
    assert con.execute("SELECT COUNT(*) FROM execution_workflows").fetchone()[0] == 1


def test_next_cycle_submission_requires_confirm_demo():
    con = _db()
    broker = FakeBroker()
    overlay = _overlay(mode="CRASH", target=0.1, trough=700.0)
    first = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=overlay,
    )
    assert first.action == "WORKFLOW_CREATED"
    second = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=overlay,
    )
    assert second.action == "SUBMISSION_BLOCKED_CONFIRM_REQUIRED"
    assert broker.posts == []


def test_confirmed_second_cycle_posts_after_durable_workflow():
    con = _db()
    broker = FakeBroker()
    overlay = _overlay(mode="CRASH", target=0.1, trough=700.0)
    first = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=overlay,
    )
    second = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=overlay,
        confirm=True,
    )
    assert first.action == "WORKFLOW_CREATED"
    assert second.action == "SELL_SUBMITTED"
    assert broker.posts
    assert all(
        row[0] == "BROKER_ACCEPTED"
        for row in con.execute(
            "SELECT status FROM execution_legs WHERE workflow_id=?",
            (first.workflow_id,),
        ).fetchall()
    )


def test_same_set_new_month_advances_without_rebalance():
    con = _db()
    broker = FakeBroker()
    result = _run(
        con,
        broker,
        now=CLOSED,
        membership=_membership("2026-08", ("NVDA", "AAPL")),
        overlay=_overlay(),
    )
    assert result.action == "NO_TRADE_CONTROL_COMMITTED"
    state = con.execute(
        """
        SELECT membership_state,active_membership_month,active_membership_json
        FROM machine_state WHERE id=1
        """
    ).fetchone()
    assert state[0] == "ACTIVE"
    assert state[1] == "2026-08"
    assert set(json.loads(state[2])["symbols"]) == {"AAPL", "NVDA"}


def test_changed_month_is_pending_outside_session():
    con = _db()
    broker = FakeBroker()
    result = _run(
        con,
        broker,
        now=CLOSED,
        membership=_membership("2026-08", ("AAPL", "MSFT")),
        overlay=_overlay(),
    )
    assert result.action == "WAIT_REGULAR_SESSION"
    assert con.execute(
        "SELECT membership_state,active_membership_month FROM machine_state WHERE id=1"
    ).fetchone() == ("REBALANCE_PENDING", "2026-07")


def test_changed_membership_regular_session_creates_rebalance_workflow():
    con = _db()
    broker = FakeBroker()
    result = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership("2026-08", ("AAPL", "MSFT")),
        overlay=_overlay(),
    )
    assert result.action == "WORKFLOW_CREATED"
    payload = json.loads(
        con.execute(
            "SELECT target_payload FROM execution_workflows WHERE workflow_id=?",
            (result.workflow_id,),
        ).fetchone()[0]
    )["decision"]
    assert payload["membership_requires_rebalance"] is True
    assert payload["target_weights"]["NVDA"] == 0.0


def test_complete_workflow_is_finalized_in_later_cycle():
    con = _db()
    broker = FakeBroker()
    decision = {
        "schema": "m7_cycle_v1",
        "decision_id": "m7:test:complete",
        "event": "ROBUST_OVERLAY_CHANGE",
        "membership": {
            "month": "2026-07",
            "symbols": ["AAPL", "NVDA"],
        },
        "previous_membership": {
            "month": "2026-07",
            "symbols": ["AAPL", "NVDA"],
        },
        "membership_requires_rebalance": False,
        "overlay": {
            "as_of": "2026-08-12",
            "ivv_close": 700.0,
            "mode": "CRASH",
            "old_peak": 800.0,
            "trough": 700.0,
            "drawdown": -0.125,
            "recovery": 0.0,
            "target_sp500": 0.1,
            "last_event": "ROTATE_DD",
        },
        "target_weights": {"AAPL": 0.45, "NVDA": 0.45, "VUAA": 0.1},
        "sp2_mix_after": {"AAPL": 0.5, "NVDA": 0.5},
        "strategy_broker_tickers": ["AAPL_US_EQ", "NVDA_US_EQ", "VUAAm_EQ"],
        "valuation_symbols": ["AAPL", "NVDA"],
        "strategy_nav_eur": 10000.0,
        "source_state_revision": 1,
    }
    con.execute(
        """
        INSERT INTO execution_workflows(
            workflow_id,decision_id,kind,status,phase,source_state_revision,
            target_payload,created_at,updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "m5b:m7:test:complete",
            decision["decision_id"],
            "TWO_PHASE_REBALANCE",
            "COMPLETE",
            "RECONCILE",
            1,
            json.dumps(
                {
                    "schema": "m5b_two_phase_broker_executor_v2",
                    "decision": decision,
                    "target_weights": decision["target_weights"],
                    "strategy_broker_tickers": decision["strategy_broker_tickers"],
                    "source_positions_by_ticker": {},
                    "initial_fresh_plan": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "2026-08-13T10:00:00+00:00",
            "2026-08-13T10:01:00+00:00",
        ),
    )
    con.commit()
    result = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=_overlay(),
    )
    assert result.action == "CONTROL_FINALIZED"
    assert broker.posts == []
    assert con.execute(
        "SELECT strategy_state,active_overlay FROM machine_state WHERE id=1"
    ).fetchone() == ("CRASH", 0.1)


def test_post_handoff_commit_sets_rearm_old_ath_to_old_peak():
    con = _db(overlay=0.1, strategy_state="CRASH")
    decision = {
        "schema": "m7_cycle_v1",
        "decision_id": "m7:test:handoff",
        "event": "ROBUST_OVERLAY_CHANGE",
        "membership": {
            "month": "2026-07",
            "symbols": ["AAPL", "NVDA"],
        },
        "membership_requires_rebalance": False,
        "overlay": {
            "as_of": "2026-08-12",
            "ivv_close": 760.0,
            "mode": "POST_HANDOFF",
            "old_peak": 800.0,
            "trough": 600.0,
            "drawdown": -0.05,
            "recovery": 0.8,
            "target_sp500": 0.0,
            "last_event": "HANDOFF",
        },
        "sp2_mix_after": {"AAPL": 0.6, "NVDA": 0.4},
    }
    _apply_control_target(
        con,
        decision=decision,
        created_at="2026-08-13T12:00:00+00:00",
    )
    assert con.execute(
        "SELECT strategy_state,rearm_old_ath,old_peak FROM machine_state WHERE id=1"
    ).fetchone() == ("POST_HANDOFF", 800.0, 800.0)


def test_cycle_does_not_import_legacy_journal():
    import sp1execution.execution.cycle_v04 as cycle

    source = Path(cycle.__file__).read_text()
    assert "state.journal" not in source
    assert "Journal(" not in source


def test_post_handoff_rearm_is_local_no_trade_outside_session():
    con = _db(
        overlay=0.0,
        strategy_state="POST_HANDOFF",
    )
    con.execute(
        """
        UPDATE machine_state
        SET
            old_peak=800.0,
            trough=600.0,
            rearm_old_ath=800.0
        WHERE id=1
        """
    )
    con.commit()

    broker = FakeBroker()

    result = _run(
        con,
        broker,
        now=CLOSED,
        membership=_membership(),
        overlay=_overlay(
            mode="NORMAL",
            target=0.0,
            old_peak=805.0,
            trough=None,
        ),
    )

    assert result.action == "NO_TRADE_CONTROL_COMMITTED"
    assert broker.posts == []

    state = con.execute(
        """
        SELECT
            strategy_state,
            active_overlay,
            old_peak,
            trough,
            rearm_old_ath
        FROM machine_state
        WHERE id=1
        """
    ).fetchone()

    assert state == (
        "NORMAL",
        0.0,
        805.0,
        None,
        None,
    )


def test_submission_guard_rejects_same_date_but_changed_overlay_snapshot():
    con = _db()
    broker = FakeBroker()

    original = _overlay(
        mode="CRASH",
        target=0.1,
        as_of="2026-08-12",
        old_peak=800.0,
        trough=700.0,
    )

    first = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=original,
    )
    assert first.action == "WORKFLOW_CREATED"
    assert broker.posts == []

    class QuoteMarket:
        def quote(self, symbol):
            return SimpleNamespace(
                age_seconds=1.0,
                price=100.0,
                currency="EUR" if symbol == "VUAA.DE" else "USD",
            )

    changed_same_date = _overlay(
        mode="CRASH",
        target=0.3,
        as_of="2026-08-12",
        old_peak=800.0,
        trough=690.0,
    )

    with pytest.raises(
        CycleSafetyError,
        match="signal snapshot is stale",
    ):
        run_cycle(
            con,
            broker=broker,
            market=QuoteMarket(),
            allow_demo_submit=True,
            now_fn=lambda: REGULAR,
            membership_loader=lambda: _membership(),
            overlay_loader=lambda market: changed_same_date,
            snapshot_builder=_snapshot,
        )

    assert broker.posts == []


def test_submission_guard_accepts_exact_overlay_snapshot_then_posts():
    con = _db()
    broker = FakeBroker()

    original = _overlay(
        mode="CRASH",
        target=0.1,
        as_of="2026-08-12",
        old_peak=800.0,
        trough=700.0,
    )

    first = _run(
        con,
        broker,
        now=REGULAR,
        membership=_membership(),
        overlay=original,
    )
    assert first.action == "WORKFLOW_CREATED"

    class QuoteMarket:
        def quote(self, symbol):
            return SimpleNamespace(
                age_seconds=1.0,
                price=100.0,
                currency="EUR" if symbol == "VUAA.DE" else "USD",
            )

    second = run_cycle(
        con,
        broker=broker,
        market=QuoteMarket(),
        allow_demo_submit=True,
        now_fn=lambda: REGULAR,
        membership_loader=lambda: _membership(),
        overlay_loader=lambda market: original,
        snapshot_builder=_snapshot,
    )

    assert second.action == "SELL_SUBMITTED"
    assert broker.posts
