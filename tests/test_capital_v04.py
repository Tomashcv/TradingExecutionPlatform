from __future__ import annotations

import sqlite3

import pytest

from sp1execution.broker.history_v04 import parse_history_item
from sp1execution.state.capital_v04 import (
    CapitalLedgerError,
    apply_fill_event,
    normalize_order_fills,
)


def _history_item(
    *,
    oid=90000000001,
    fill_id=91000000001,
    ticker="AAPL_US_EQ",
    side="BUY",
    quantity=50.0,
    price=100.0,
    net_value=5000.0,
    fee=-5.0,
):
    signed_quantity = quantity if side == "BUY" else -abs(quantity)

    return {
        "order": {
            "createdAt": "2026-08-13T01:13:21.000Z",
            "filledQuantity": signed_quantity,
            "id": oid,
            "quantity": signed_quantity,
            "side": side,
            "status": "FILLED",
            "ticker": ticker,
            "type": "MARKET",
        },
        "fill": {
            "filledAt": "2026-08-13T13:30:00.000Z",
            "id": fill_id,
            "price": price,
            "quantity": signed_quantity,
            "walletImpact": {
                "currency": "EUR",
                "fxRate": 1.15348061,
                "netValue": net_value,
                "taxes": [
                    {
                        "chargedAt": "2026-08-13T13:30:06.747Z",
                        "currency": "EUR",
                        "name": "CURRENCY_CONVERSION_FEE",
                        "quantity": fee,
                    }
                ],
            },
        },
    }


def _db(
    *,
    cash=10000.0,
    debt=0.0,
    fees=0.0,
):
    con = sqlite3.connect(":memory:")

    con.executescript(
        """
        CREATE TABLE machine_state (
            id INTEGER PRIMARY KEY,
            revision INTEGER NOT NULL,
            strategy_cash_eur REAL NOT NULL,
            external_cash_debt_eur REAL NOT NULL,
            realized_fees_eur REAL NOT NULL,
            realized_fx_eur REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE capital_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            decision_id TEXT,
            broker_order_id TEXT,
            fill_id TEXT,
            event_type TEXT NOT NULL,
            cash_delta_eur REAL NOT NULL,
            fee_eur REAL NOT NULL DEFAULT 0.0,
            fx_rate REAL,
            payload TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    con.execute(
        """
        INSERT INTO machine_state(
            id,
            revision,
            strategy_cash_eur,
            external_cash_debt_eur,
            realized_fees_eur,
            realized_fx_eur,
            updated_at
        )
        VALUES(1,1,?,?,?,?,?)
        """,
        (
            cash,
            debt,
            fees,
            0.0,
            "2026-08-13T00:00:00+00:00",
        ),
    )

    con.commit()
    return con


def test_synthetic_buy_normalizes_exact_wallet_debit():
    order = parse_history_item(_history_item())
    event = normalize_order_fills(order)[0]

    assert event.cash_delta_eur == -5000.0
    assert event.fee_eur == 5.0
    assert event.wallet_net_value_eur == 5000.0
    assert event.event_key == "t212:fill:90000000001:91000000001"


def test_synthetic_two_fill_bootstrap_totals():
    aapl = parse_history_item(_history_item())

    nvda = parse_history_item(
        _history_item(
            oid=90000000002,
            fill_id=91000000002,
            ticker="NVDA_US_EQ",
            quantity=25.0,
            price=200.0,
            net_value=5000.0,
            fee=-5.0,
        )
    )

    events = normalize_order_fills(aapl) + normalize_order_fills(nvda)

    assert round(sum(x.cash_delta_eur for x in events), 2) == -10000.0
    assert round(sum(x.fee_eur for x in events), 2) == 10.0


def test_buy_wallet_net_value_already_includes_fee_no_double_subtract():
    con = _db(cash=10000.0)

    event = normalize_order_fills(parse_history_item(_history_item()))[0]

    result = apply_fill_event(con, event)

    assert result.new_strategy_cash_eur == 5000.0
    assert result.new_realized_fees_eur == 5.0

    row = con.execute(
        """
        SELECT cash_delta_eur, fee_eur
        FROM capital_ledger
        WHERE event_key=?
        """,
        (event.event_key,),
    ).fetchone()

    assert row == (-5000.0, 5.0)


def test_sell_credit_repays_existing_external_debt_by_mirror():
    con = _db(cash=-50.0, debt=50.0, fees=10.0)

    order = parse_history_item(
        _history_item(
            side="SELL",
            quantity=50.0,
            net_value=5000.00,
            fee=-5.00,
        )
    )

    event = normalize_order_fills(order)[0]
    assert event.cash_delta_eur == 5000.00

    result = apply_fill_event(con, event)

    assert result.new_strategy_cash_eur == 4950.0
    assert result.new_external_cash_debt_eur == 0.0
    assert result.external_debt_change_eur == -50.0
    assert result.new_realized_fees_eur == 15.0


def test_partial_sell_credit_keeps_only_remaining_negative_cash_as_debt():
    con = _db(cash=-50.0, debt=50.0)

    order = parse_history_item(
        _history_item(
            side="SELL",
            quantity=1.0,
            net_value=20.00,
            fee=0.00,
        )
    )

    result = apply_fill_event(
        con,
        normalize_order_fills(order)[0],
    )

    assert result.new_strategy_cash_eur == -30.0
    assert result.new_external_cash_debt_eur == 30.0


def test_observed_buy_overshoot_is_recorded_not_hidden():
    con = _db(cash=100.00, debt=0.0)

    order = parse_history_item(
        _history_item(
            quantity=1.0,
            net_value=105.00,
            fee=-1.00,
        )
    )

    result = apply_fill_event(
        con,
        normalize_order_fills(order)[0],
    )

    assert result.new_strategy_cash_eur == -5.00
    assert result.new_external_cash_debt_eur == 5.00
    assert result.external_debt_change_eur == 5.00


def test_exact_fill_replay_is_idempotent_and_revision_does_not_change():
    con = _db(cash=10000.0)
    event = normalize_order_fills(parse_history_item(_history_item()))[0]

    first = apply_fill_event(
        con,
        event,
        decision_id="decision-1",
    )
    second = apply_fill_event(
        con,
        event,
        decision_id="decision-1",
    )

    assert first.status == "APPLIED"
    assert second.status == "ALREADY_APPLIED"
    assert first.new_revision == 2
    assert second.new_revision == 2

    count = con.execute("SELECT COUNT(*) FROM capital_ledger").fetchone()[0]

    assert count == 1


def test_conflicting_replay_fails_closed():
    con = _db(cash=10000.0)
    event = normalize_order_fills(parse_history_item(_history_item()))[0]

    apply_fill_event(
        con,
        event,
        decision_id="decision-1",
    )

    with pytest.raises(CapitalLedgerError, match="Conflicting replay"):
        apply_fill_event(
            con,
            event,
            decision_id="different-decision",
        )


def test_non_eur_wallet_impact_fails_closed():
    raw = _history_item()
    raw["fill"]["walletImpact"]["currency"] = "USD"

    order = parse_history_item(raw)

    with pytest.raises(CapitalLedgerError, match="EUR wallet"):
        normalize_order_fills(order)


def test_non_eur_fee_fails_closed():
    raw = _history_item()
    raw["fill"]["walletImpact"]["taxes"][0]["currency"] = "USD"

    order = parse_history_item(raw)

    with pytest.raises(CapitalLedgerError, match="EUR fee"):
        normalize_order_fills(order)


def test_missing_wallet_net_value_fails_closed():
    raw = _history_item()
    raw["fill"]["walletImpact"]["netValue"] = None

    order = parse_history_item(raw)

    with pytest.raises(CapitalLedgerError, match="netValue"):
        normalize_order_fills(order)


def test_unknown_side_fails_closed():
    raw = _history_item()
    raw["order"]["side"] = "ALIEN"

    order = parse_history_item(raw)

    with pytest.raises(CapitalLedgerError, match="Unsupported"):
        normalize_order_fills(order)
