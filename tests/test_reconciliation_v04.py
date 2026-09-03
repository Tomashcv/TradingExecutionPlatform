from __future__ import annotations

import pytest

from sp1execution.broker.history_v04 import HistorySchemaError
from sp1execution.engine.reconciliation_v04 import reconcile_accepted_attempts


def _attempt(
    *,
    oid="90000000001",
    ticker="AAPL_US_EQ",
    quantity=50.0,
):
    return {
        "broker_order_id": oid,
        "ticker": ticker,
        "quantity": quantity,
    }


def _history_item(
    *,
    oid=90000000001,
    ticker="AAPL_US_EQ",
    quantity=50.0,
    filled=50.0,
    status="FILLED",
):
    return {
        "order": {
            "createdAt": "2026-08-13T01:13:21.000Z",
            "filledQuantity": filled,
            "id": oid,
            "quantity": quantity,
            "side": "BUY" if quantity >= 0 else "SELL",
            "status": status,
            "ticker": ticker,
            "type": "MARKET",
        },
        "fill": {
            "filledAt": "2026-08-13T13:30:00.000Z",
            "id": oid + 1000,
            "price": 304.26,
            "quantity": filled,
            "walletImpact": {
                "currency": "EUR",
                "fxRate": 1.15348061,
                "netValue": 5000.0,
                "taxes": [],
            },
        },
    }


def test_nested_history_fill_wins_and_is_filled():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [_history_item()],
            "nextPagePath": None,
        }
    }

    rows = reconcile_accepted_attempts(
        attempts=[_attempt()],
        pending_payload=[],
        fetch_history_page=lambda path: pages[path],
    )

    assert rows[0].state == "FILLED"
    assert rows[0].filled_quantity == 50.0
    assert rows[0].broker_status == "FILLED"
    assert rows[0].evidence_source == "HISTORICAL_NESTED_ORDER_FILL"


def test_synthetic_two_orders_both_fill():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [
                _history_item(),
                _history_item(
                    oid=90000000002,
                    ticker="NVDA_US_EQ",
                    quantity=25.0,
                    filled=25.0,
                ),
            ],
            "nextPagePath": None,
        }
    }

    rows = reconcile_accepted_attempts(
        attempts=[
            _attempt(),
            _attempt(
                oid="90000000002",
                ticker="NVDA_US_EQ",
                quantity=25.0,
            ),
        ],
        pending_payload=[],
        fetch_history_page=lambda path: pages[path],
    )

    assert [row.state for row in rows] == ["FILLED", "FILLED"]


def test_history_on_second_page_is_found():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [],
            "nextPagePath": "/page2",
        },
        "/page2": {
            "items": [_history_item()],
            "nextPagePath": None,
        },
    }

    rows = reconcile_accepted_attempts(
        attempts=[_attempt()],
        pending_payload=[],
        fetch_history_page=lambda path: pages[path],
    )

    assert rows[0].state == "FILLED"


def test_history_ticker_mismatch_fails_closed():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [_history_item(ticker="NVDA_US_EQ")],
            "nextPagePath": None,
        }
    }

    with pytest.raises(HistorySchemaError, match="ticker mismatch"):
        reconcile_accepted_attempts(
            attempts=[_attempt()],
            pending_payload=[],
            fetch_history_page=lambda path: pages[path],
        )


def test_history_quantity_mismatch_fails_closed():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [_history_item(quantity=20.0, filled=20.0)],
            "nextPagePath": None,
        }
    }

    with pytest.raises(HistorySchemaError, match="quantity mismatch"):
        reconcile_accepted_attempts(
            attempts=[_attempt()],
            pending_payload=[],
            fetch_history_page=lambda path: pages[path],
        )


def test_absent_from_pending_and_history_is_unknown():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [],
            "nextPagePath": None,
        }
    }

    rows = reconcile_accepted_attempts(
        attempts=[_attempt()],
        pending_payload=[],
        fetch_history_page=lambda path: pages[path],
    )

    assert rows[0].state == "UNKNOWN"
    assert rows[0].evidence_source == "ABSENT_FROM_PENDING_AND_HISTORY"


def test_missing_broker_id_is_unknown_without_guessing():
    pages = {
        "/equity/history/orders?limit=50": {
            "items": [],
            "nextPagePath": None,
        }
    }

    rows = reconcile_accepted_attempts(
        attempts=[
            {
                "broker_order_id": None,
                "ticker": "AAPL_US_EQ",
                "quantity": 50.0,
            }
        ],
        pending_payload=[],
        fetch_history_page=lambda path: pages[path],
    )

    assert rows[0].state == "UNKNOWN"
    assert rows[0].evidence_source == "MISSING_BROKER_ORDER_ID"
