from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from sp1execution.engine.planner import InstrumentQuote, PlannedOrder
from sp1execution.execution.broker_executor_v04 import (
    BrokerExecutorError,
    BrokerSubmissionAmbiguous,
    CashBoundaryBreach,
    FreshSellRequired,
    PhaseNotReady,
    PositionReconciliationError,
    create_and_start_from_fresh_orders,
    reconcile_current_phase,
    replan_and_install_buys,
    submit_current_phase,
    workflow_id_for_decision,
)
from sp1execution.state.v04_store import ensure_schema

NOW = "2026-08-13T21:30:00+00:00"


def _db(*, cash=-50.0, debt=50.0):
    con = sqlite3.connect(":memory:")
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
            0.0,'{"AAPL":0.5,"NVDA":0.5}',NULL,NULL,NULL,
            10000.0,?,?,10.0,0.0,NULL,?,?
        )
        """,
        (cash, debt, NOW, NOW),
    )
    con.commit()
    return con


def _state(con):
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM machine_state WHERE id=1").fetchone()
    con.row_factory = None
    return dict(row)


def _workflow(con, workflow_id):
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM execution_workflows WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    con.row_factory = None
    return dict(row)


def _legs(con, workflow_id):
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT * FROM execution_legs
        WHERE workflow_id=?
        ORDER BY leg_index
        """,
        (workflow_id,),
    ).fetchall()
    con.row_factory = None
    return [dict(row) for row in rows]


def _sell_order(quantity=-10.0, notional=1000.0):
    return PlannedOrder(
        logical_symbol="NVDA",
        broker_ticker="NVDA_US_EQ",
        quantity=quantity,
        side="SELL",
        estimated_notional_eur=notional,
        delta_eur=-notional,
    )


def _decision(target_weights=None):
    if target_weights is None:
        target_weights = {"NVDA": 0.0, "VUAA": 1.0}
    return {
        "decision_id": "decision-1",
        "target_weights": target_weights,
        "strategy_broker_tickers": [
            "AAPL_US_EQ",
            "NVDA_US_EQ",
            "VUAAm_EQ",
        ],
    }


def _history_item(
    *,
    oid,
    fill_id,
    ticker,
    side,
    quantity,
    net_value,
    fee=-1.0,
    status="FILLED",
):
    signed = abs(quantity) if side == "BUY" else -abs(quantity)
    return {
        "order": {
            "createdAt": NOW,
            "filledQuantity": signed,
            "id": oid,
            "quantity": signed,
            "side": side,
            "status": status,
            "ticker": ticker,
            "type": "MARKET",
        },
        "fill": {
            "filledAt": NOW,
            "id": fill_id,
            "price": 100.0,
            "quantity": signed,
            "walletImpact": {
                "currency": "EUR",
                "fxRate": 1.15,
                "netValue": net_value,
                "taxes": [
                    {
                        "chargedAt": NOW,
                        "currency": "EUR",
                        "name": "CURRENCY_CONVERSION_FEE",
                        "quantity": fee,
                    }
                ],
            },
        },
    }


class FakeBroker:
    def __init__(self, *, env="demo", positions=None, history_items=None):
        self.settings = SimpleNamespace(t212_env=env)
        self._positions = list(positions or [])
        self._pending = []
        self._history_items = list(history_items or [])
        self.submissions = []
        self.on_submit = None
        self.raise_on_submit = None
        self.next_id = 1000

    def pending_orders(self):
        return list(self._pending)

    def positions(self, force_refresh=False):
        return list(self._positions)

    def market_order_demo_only(self, ticker, quantity):
        self.submissions.append((ticker, quantity))
        if self.on_submit is not None:
            self.on_submit(ticker, quantity)
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        self.next_id += 1
        return {
            "id": str(self.next_id),
            "ticker": ticker,
            "quantity": quantity,
            "status": "NEW",
        }

    def _request(self, method, path):
        assert method == "GET"
        assert path == "/equity/history/orders?limit=50"
        return {
            "items": list(self._history_items),
            "nextPagePath": None,
        }


def _create_sell_workflow(con, *, positions=None):
    if positions is None:
        positions = [{"ticker": "NVDA_US_EQ", "quantity": 10.0}]
    snapshot = create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision(),
        positions=positions,
        orders=[_sell_order()],
        created_at=NOW,
    )
    return workflow_id_for_decision("decision-1"), snapshot


def test_create_from_fresh_plan_persists_only_sell_legs_initially():
    con = _db()
    workflow_id, snapshot = _create_sell_workflow(con)

    assert snapshot.phase == "SELL"
    assert snapshot.execution_state == "SELL_PENDING"

    legs = _legs(con, workflow_id)
    assert len(legs) == 1
    assert legs[0]["side"] == "SELL"
    assert legs[0]["intended_quantity"] == 10.0


def test_submission_records_intent_before_post():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()

    def observe(ticker, quantity):
        leg = _legs(con, workflow_id)[0]
        assert leg["status"] == "INTENT_RECORDED"
        assert leg["broker_order_id"] is None
        assert ticker == "NVDA_US_EQ"
        assert quantity == -10.0

    broker.on_submit = observe

    result = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )

    assert len(result.broker_order_ids) == 1
    leg = _legs(con, workflow_id)[0]
    assert leg["status"] == "BROKER_ACCEPTED"
    assert leg["broker_order_id"] == result.broker_order_ids[0]


def test_post_exception_marks_reconciliation_required_and_never_retries():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()
    broker.raise_on_submit = RuntimeError("network died after POST")

    with pytest.raises(RuntimeError, match="network died"):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )

    assert _state(con)["execution_state"] == "RECONCILIATION_REQUIRED"
    assert _workflow(con, workflow_id)["status"] == "RECONCILIATION_REQUIRED"

    with pytest.raises(PhaseNotReady):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )

    assert len(broker.submissions) == 1


def test_live_environment_is_refused_before_post():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker(env="live")

    with pytest.raises(BrokerExecutorError, match="Demo-only"):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )

    assert broker.submissions == []


def test_unrelated_pending_order_blocks_new_submission():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()
    broker._pending = [{"id": "other-strategy-order"}]

    with pytest.raises(BrokerExecutorError, match="not owned by this workflow"):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )

    assert broker.submissions == []


def test_sell_fill_applies_m3_cash_repays_debt_and_enters_reconcile():
    con = _db(cash=-50.0, debt=50.0)
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()

    submission = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    oid = submission.broker_order_ids[0]

    broker._history_items = [
        _history_item(
            oid=oid,
            fill_id="fill-sell-1",
            ticker="NVDA_US_EQ",
            side="SELL",
            quantity=10.0,
            net_value=1000.0,
        )
    ]
    broker._positions = []

    result = reconcile_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        created_at=NOW,
    )

    assert result.snapshot.execution_state == "RECONCILING"
    state = _state(con)
    assert state["strategy_cash_eur"] == 950.0
    assert state["external_cash_debt_eur"] == 0.0
    assert state["realized_fees_eur"] == 11.0

    row = con.execute("SELECT COUNT(*) FROM capital_ledger WHERE fill_id='fill-sell-1'").fetchone()
    assert row[0] == 1


def test_position_mismatch_blocks_filled_phase_transition():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()

    submission = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    oid = submission.broker_order_ids[0]
    broker._history_items = [
        _history_item(
            oid=oid,
            fill_id="fill-sell-1",
            ticker="NVDA_US_EQ",
            side="SELL",
            quantity=10.0,
            net_value=1000.0,
        )
    ]
    broker._positions = [{"ticker": "NVDA_US_EQ", "quantity": 1.0}]

    with pytest.raises(PositionReconciliationError):
        reconcile_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            created_at=NOW,
        )

    assert _workflow(con, workflow_id)["phase"] == "SELL"
    assert _state(con)["execution_state"] == "SELL_PENDING"


def test_repeated_fill_reconciliation_is_cash_idempotent():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()

    submission = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    oid = submission.broker_order_ids[0]
    broker._history_items = [
        _history_item(
            oid=oid,
            fill_id="fill-sell-1",
            ticker="NVDA_US_EQ",
            side="SELL",
            quantity=10.0,
            net_value=1000.0,
        )
    ]
    broker._positions = [{"ticker": "NVDA_US_EQ", "quantity": 1.0}]

    for _ in range(2):
        with pytest.raises(PositionReconciliationError):
            reconcile_current_phase(
                con,
                workflow_id=workflow_id,
                broker=broker,
                created_at=NOW,
            )

    assert _state(con)["strategy_cash_eur"] == 950.0
    row = con.execute("SELECT COUNT(*) FROM capital_ledger WHERE fill_id='fill-sell-1'").fetchone()
    assert row[0] == 1


def test_replan_buys_uses_durable_strategy_cash_and_installs_buy_phase():
    con = _db(cash=1000.0, debt=0.0)
    workflow_id = workflow_id_for_decision("decision-1")

    snapshot = create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision(),
        positions=[],
        orders=[],
        created_at=NOW,
    )
    assert snapshot.phase == "RECONCILE"

    quotes = {
        "NVDA": InstrumentQuote(
            logical_symbol="NVDA",
            broker_ticker="NVDA_US_EQ",
            price=100.0,
            currency="USD",
        ),
        "VUAA": InstrumentQuote(
            logical_symbol="VUAA",
            broker_ticker="VUAAm_EQ",
            price=100.0,
            currency="EUR",
        ),
    }

    installed, buy_orders = replan_and_install_buys(
        con,
        workflow_id=workflow_id,
        target_weights={"NVDA": 0.0, "VUAA": 1.0},
        quotes=quotes,
        positions=[],
        eurusd=1.15,
        created_at=NOW,
    )

    assert installed.phase == "BUY"
    assert installed.execution_state == "BUY_PENDING"
    assert len(buy_orders) == 1
    assert buy_orders[0].side == "BUY"
    assert buy_orders[0].estimated_notional_eur <= 1000.0


def test_replan_blocks_when_fresh_snapshot_still_requires_sell():
    con = _db(cash=100.0, debt=0.0)
    workflow_id = workflow_id_for_decision("decision-1")

    create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision({"NVDA": 0.0, "VUAA": 1.0}),
        positions=[{"ticker": "NVDA_US_EQ", "quantity": 10.0}],
        orders=[],
        created_at=NOW,
    )

    quotes = {
        "NVDA": InstrumentQuote(
            logical_symbol="NVDA",
            broker_ticker="NVDA_US_EQ",
            price=100.0,
            currency="USD",
        ),
        "VUAA": InstrumentQuote(
            logical_symbol="VUAA",
            broker_ticker="VUAAm_EQ",
            price=100.0,
            currency="EUR",
        ),
    }

    with pytest.raises(FreshSellRequired):
        replan_and_install_buys(
            con,
            workflow_id=workflow_id,
            target_weights={"NVDA": 0.0, "VUAA": 1.0},
            quotes=quotes,
            positions=[{"ticker": "NVDA_US_EQ", "quantity": 10.0}],
            eurusd=1.15,
            created_at=NOW,
        )


def test_buy_fill_requires_position_reconciliation_before_idle():
    con = _db(cash=1000.0, debt=0.0)
    workflow_id = workflow_id_for_decision("decision-1")
    create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision(),
        positions=[],
        orders=[],
        created_at=NOW,
    )

    quotes = {
        "NVDA": InstrumentQuote(
            logical_symbol="NVDA",
            broker_ticker="NVDA_US_EQ",
            price=100.0,
            currency="USD",
        ),
        "VUAA": InstrumentQuote(
            logical_symbol="VUAA",
            broker_ticker="VUAAm_EQ",
            price=100.0,
            currency="EUR",
        ),
    }

    _, buy_orders = replan_and_install_buys(
        con,
        workflow_id=workflow_id,
        target_weights={"NVDA": 0.0, "VUAA": 1.0},
        quotes=quotes,
        positions=[],
        eurusd=1.15,
        created_at=NOW,
    )

    broker = FakeBroker()
    submission = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    oid = submission.broker_order_ids[0]
    quantity = abs(float(buy_orders[0].quantity))

    broker._history_items = [
        _history_item(
            oid=oid,
            fill_id="fill-buy-1",
            ticker="VUAAm_EQ",
            side="BUY",
            quantity=quantity,
            net_value=buy_orders[0].estimated_notional_eur,
        )
    ]
    broker._positions = [{"ticker": "VUAAm_EQ", "quantity": quantity}]

    result = reconcile_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        created_at=NOW,
    )

    assert result.snapshot.status == "COMPLETE"
    assert result.snapshot.execution_state == "IDLE"
    assert _state(con)["external_cash_debt_eur"] == 0.0


def test_phase_cannot_reconcile_before_every_leg_is_submitted():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()

    with pytest.raises(PhaseNotReady, match="not every leg has been submitted"):
        reconcile_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            created_at=NOW,
        )


def test_missing_order_id_response_is_ambiguous_and_blocks_retry():
    con = _db()
    workflow_id, _ = _create_sell_workflow(con)
    broker = FakeBroker()

    def missing_id(ticker, quantity):
        broker.submissions.append((ticker, quantity))
        return {"ticker": ticker, "quantity": quantity, "status": "NEW"}

    broker.market_order_demo_only = missing_id

    with pytest.raises(BrokerSubmissionAmbiguous):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )

    assert _state(con)["execution_state"] == "RECONCILIATION_REQUIRED"


def _two_buy_setup(*, cash=1000.0):
    con = _db(cash=cash, debt=0.0)
    workflow_id = workflow_id_for_decision("decision-1")
    create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision({"AAPL": 0.5, "VUAA": 0.5}),
        positions=[],
        orders=[],
        created_at=NOW,
    )
    quotes = {
        "AAPL": InstrumentQuote(
            logical_symbol="AAPL",
            broker_ticker="AAPL_US_EQ",
            price=100.0,
            currency="EUR",
        ),
        "VUAA": InstrumentQuote(
            logical_symbol="VUAA",
            broker_ticker="VUAAm_EQ",
            price=100.0,
            currency="EUR",
        ),
    }
    _, buy_orders = replan_and_install_buys(
        con,
        workflow_id=workflow_id,
        target_weights={"AAPL": 0.5, "VUAA": 0.5},
        quotes=quotes,
        positions=[],
        eurusd=1.15,
        created_at=NOW,
    )
    assert len(buy_orders) == 2
    return con, workflow_id, buy_orders


def test_two_buy_legs_submit_one_at_a_time_using_realized_cash():
    con, workflow_id, buy_orders = _two_buy_setup()
    broker = FakeBroker()

    first = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    assert len(first.broker_order_ids) == 1
    assert len(broker.submissions) == 1

    with pytest.raises(PhaseNotReady, match="already in flight"):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )
    assert len(broker.submissions) == 1

    first_order = buy_orders[0]
    first_oid = first.broker_order_ids[0]
    first_qty = abs(float(first_order.quantity))
    first_cost = float(first_order.estimated_notional_eur)

    broker._history_items = [
        _history_item(
            oid=first_oid,
            fill_id="fill-buy-seq-1",
            ticker=first_order.broker_ticker,
            side="BUY",
            quantity=first_qty,
            net_value=first_cost,
        )
    ]
    broker._positions = [{"ticker": first_order.broker_ticker, "quantity": first_qty}]

    first_recon = reconcile_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        created_at=NOW,
    )
    assert first_recon.snapshot.status == "ACTIVE"
    assert first_recon.snapshot.phase == "BUY"
    assert first_recon.snapshot.execution_state == "BUY_PENDING"

    legs = _legs(con, workflow_id)
    assert [row["status"] for row in legs if row["side"] == "BUY"] == [
        "FILLED",
        "PLANNED",
    ]

    second = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    assert len(second.broker_order_ids) == 1
    assert len(broker.submissions) == 2

    second_order = buy_orders[1]
    second_oid = second.broker_order_ids[0]
    second_qty = abs(float(second_order.quantity))
    second_cost = float(second_order.estimated_notional_eur)

    broker._history_items.append(
        _history_item(
            oid=second_oid,
            fill_id="fill-buy-seq-2",
            ticker=second_order.broker_ticker,
            side="BUY",
            quantity=second_qty,
            net_value=second_cost,
        )
    )
    broker._positions = [
        {"ticker": first_order.broker_ticker, "quantity": first_qty},
        {"ticker": second_order.broker_ticker, "quantity": second_qty},
    ]

    final = reconcile_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        created_at=NOW,
    )
    assert final.snapshot.status == "COMPLETE"
    assert final.snapshot.execution_state == "IDLE"
    assert _state(con)["external_cash_debt_eur"] == 0.0


def test_second_buy_is_blocked_if_first_real_fill_used_too_much_cash():
    con, workflow_id, buy_orders = _two_buy_setup()
    broker = FakeBroker()

    first = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    first_order = buy_orders[0]
    first_oid = first.broker_order_ids[0]
    first_qty = abs(float(first_order.quantity))

    # Still below total available cash, so no debt is created, but the
    # remaining durable cash is deliberately smaller than the second planned BUY.
    broker._history_items = [
        _history_item(
            oid=first_oid,
            fill_id="fill-buy-expensive-1",
            ticker=first_order.broker_ticker,
            side="BUY",
            quantity=first_qty,
            net_value=510.0,
        )
    ]
    broker._positions = [{"ticker": first_order.broker_ticker, "quantity": first_qty}]

    reconcile_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        created_at=NOW,
    )

    cash_after = float(_state(con)["strategy_cash_eur"])
    assert cash_after == 490.0

    with pytest.raises(PhaseNotReady, match="exceeds durable strategy cash"):
        submit_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            now_fn=lambda: NOW,
        )

    assert len(broker.submissions) == 1


def test_real_buy_fill_that_creates_debt_stops_fail_closed():
    con = _db(cash=500.0, debt=0.0)
    workflow_id = workflow_id_for_decision("decision-1")
    create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision({"VUAA": 1.0}),
        positions=[],
        orders=[],
        created_at=NOW,
    )
    quotes = {
        "VUAA": InstrumentQuote(
            logical_symbol="VUAA",
            broker_ticker="VUAAm_EQ",
            price=100.0,
            currency="EUR",
        )
    }
    _, buy_orders = replan_and_install_buys(
        con,
        workflow_id=workflow_id,
        target_weights={"VUAA": 1.0},
        quotes=quotes,
        positions=[],
        eurusd=1.15,
        created_at=NOW,
    )

    broker = FakeBroker()
    submitted = submit_current_phase(
        con,
        workflow_id=workflow_id,
        broker=broker,
        now_fn=lambda: NOW,
    )
    oid = submitted.broker_order_ids[0]
    quantity = abs(float(buy_orders[0].quantity))

    broker._history_items = [
        _history_item(
            oid=oid,
            fill_id="fill-buy-debt-breach",
            ticker="VUAAm_EQ",
            side="BUY",
            quantity=quantity,
            net_value=505.0,
        )
    ]
    broker._positions = [{"ticker": "VUAAm_EQ", "quantity": quantity}]

    with pytest.raises(CashBoundaryBreach, match="stopped fail-closed"):
        reconcile_current_phase(
            con,
            workflow_id=workflow_id,
            broker=broker,
            created_at=NOW,
        )

    assert _state(con)["external_cash_debt_eur"] == 5.0
    assert _state(con)["execution_state"] == "RECONCILIATION_REQUIRED"
    assert _workflow(con, workflow_id)["status"] == "RECONCILIATION_REQUIRED"


def test_source_position_scope_ignores_unrelated_account_instruments():
    con = _db()
    workflow_id = workflow_id_for_decision("decision-1")

    create_and_start_from_fresh_orders(
        con,
        decision_id="decision-1",
        decision_payload=_decision(),
        positions=[
            {"ticker": "NVDA_US_EQ", "quantity": 10.0},
            {"ticker": "TSLA_US_EQ", "quantity": 999.0},
        ],
        orders=[_sell_order()],
        created_at=NOW,
    )

    raw = _workflow(con, workflow_id)["target_payload"]
    payload = __import__("json").loads(raw)

    assert payload["source_positions_by_ticker"] == {"NVDA_US_EQ": 10.0}
    assert "TSLA_US_EQ" not in payload["strategy_broker_tickers"]


def test_decision_payload_requires_explicit_strategy_ticker_scope():
    con = _db()
    payload = {
        "decision_id": "decision-1",
        "target_weights": {"NVDA": 0.0, "VUAA": 1.0},
    }

    with pytest.raises(BrokerExecutorError, match="strategy_broker_tickers"):
        create_and_start_from_fresh_orders(
            con,
            decision_id="decision-1",
            decision_payload=payload,
            positions=[],
            orders=[],
            created_at=NOW,
        )
