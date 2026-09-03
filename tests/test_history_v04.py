from __future__ import annotations

import pytest

from sp1execution.broker.history_v04 import (
    HistoryPaginationError,
    HistorySchemaError,
    assert_expected_order,
    canonical_state,
    fetch_exact_history_order,
    fetch_history_records,
    merge_records,
    parse_history_item,
    parse_history_page,
)


def _item(
    *,
    order_id=90000000001,
    fill_id=91000000001,
    ticker="AAPL_US_EQ",
    quantity=50.0,
    filled_quantity=50.0,
    status="FILLED",
    fill_quantity=None,
    price=100.0,
    net_value=5000.0,
    fee=-5.0,
):
    if fill_quantity is None:
        fill_quantity = filled_quantity

    row = {
        "order": {
            "createdAt": "2026-08-13T01:13:21.000Z",
            "filledQuantity": filled_quantity,
            "id": order_id,
            "quantity": quantity,
            "side": "BUY" if quantity >= 0 else "SELL",
            "status": status,
            "ticker": ticker,
            "type": "MARKET",
        }
    }

    if status != "NEW":
        row["fill"] = {
            "filledAt": "2026-08-13T13:30:00.000Z",
            "id": fill_id,
            "price": price,
            "quantity": fill_quantity,
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
        }

    return row


def test_synthetic_aapl_parses():
    record = parse_history_item(_item())
    assert record.broker_order_id == "90000000001"
    assert record.ticker == "AAPL_US_EQ"
    assert record.status == "FILLED"
    assert record.fills[0].wallet_net_value == 5000.0
    assert record.fills[0].fees[0].amount == -5.0


def test_synthetic_nvda_parses():
    record = parse_history_item(
        _item(
            order_id=90000000002,
            fill_id=91000000002,
            ticker="NVDA_US_EQ",
            quantity=25.0,
            filled_quantity=25.0,
            price=200.0,
            net_value=5000.0,
            fee=-5.0,
        )
    )
    assert record.broker_order_id == "90000000002"
    assert record.fills[0].price == 200.0


def test_pending_without_fill():
    record = parse_history_item(
        _item(
            status="NEW",
            filled_quantity=0.0,
            fill_quantity=0.0,
        )
    )
    assert record.fills == ()
    assert canonical_state(record) == "PENDING"


def test_filled_state():
    assert canonical_state(parse_history_item(_item())) == "FILLED"


def test_partial_state():
    record = parse_history_item(
        _item(
            quantity=10,
            filled_quantity=4,
            fill_quantity=4,
            status="PARTIALLY_FILLED",
        )
    )
    assert canonical_state(record) == "PARTIAL"


def test_cancelled_zero_fill_failed():
    row = _item(status="NEW", filled_quantity=0.0, fill_quantity=0.0)
    row["order"]["status"] = "CANCELLED"
    assert canonical_state(parse_history_item(row)) == "FAILED"


def test_unknown_status_stays_unknown():
    row = _item(status="NEW", filled_quantity=0.0, fill_quantity=0.0)
    row["order"]["status"] = "ALIEN"
    assert canonical_state(parse_history_item(row)) == "UNKNOWN"


def test_expected_order_matches():
    assert_expected_order(
        parse_history_item(_item()),
        ticker="AAPL_US_EQ",
        quantity=50.0,
    )


def test_sell_quantity_matches_by_magnitude():
    record = parse_history_item(
        _item(
            quantity=-50.0,
            filled_quantity=-50.0,
            fill_quantity=-50.0,
        )
    )
    assert_expected_order(
        record,
        ticker="AAPL_US_EQ",
        quantity=-50.0,
    )
    assert canonical_state(record, expected_quantity=-50.0) == "FILLED"


def test_wrong_ticker_fails_closed():
    with pytest.raises(HistorySchemaError, match="ticker mismatch"):
        assert_expected_order(
            parse_history_item(_item()),
            ticker="NVDA_US_EQ",
            quantity=50.0,
        )


def test_wrong_quantity_fails_closed():
    with pytest.raises(HistorySchemaError, match="quantity mismatch"):
        assert_expected_order(
            parse_history_item(_item()),
            ticker="AAPL_US_EQ",
            quantity=20.0,
        )


def test_flat_legacy_item_rejected():
    with pytest.raises(HistorySchemaError, match="order"):
        parse_history_item({"id": 1, "status": "FILLED", "ticker": "AAPL_US_EQ"})


def test_page_requires_items_list():
    with pytest.raises(HistorySchemaError, match="history.items"):
        parse_history_page({"items": None})


def test_multi_fill_merge():
    left = parse_history_item(
        _item(
            quantity=10,
            filled_quantity=4,
            fill_quantity=4,
            fill_id=1001,
            status="PARTIALLY_FILLED",
        )
    )
    right = parse_history_item(
        _item(
            quantity=10,
            filled_quantity=10,
            fill_quantity=6,
            fill_id=1002,
            status="FILLED",
        )
    )
    merged = merge_records(left, right)
    assert merged.filled_quantity == 10
    assert merged.observed_fill_quantity == 10
    assert len(merged.fills) == 2


def test_duplicate_fill_id_conflict_fails_closed():
    left = parse_history_item(_item(fill_id=1001, price=100))
    right = parse_history_item(_item(fill_id=1001, price=101))
    with pytest.raises(HistorySchemaError, match="same fill ID"):
        merge_records(left, right)


def test_second_page_lookup():
    pages = {
        "/first": {
            "items": [_item(order_id=111, fill_id=211)],
            "nextPagePath": "/second",
        },
        "/second": {
            "items": [
                _item(
                    order_id=999,
                    fill_id=299,
                    ticker="NVDA_US_EQ",
                )
            ],
            "nextPagePath": None,
        },
    }
    record = fetch_exact_history_order(
        lambda path: pages[path],
        broker_order_id="999",
        first_path="/first",
    )
    assert record is not None
    assert record.broker_order_id == "999"


def test_missing_order_returns_none():
    pages = {
        "/first": {
            "items": [_item(order_id=111, fill_id=211)],
            "nextPagePath": None,
        }
    }
    assert (
        fetch_exact_history_order(
            lambda path: pages[path],
            broker_order_id="999",
            first_path="/first",
        )
        is None
    )


def test_pagination_loop_fails_closed():
    pages = {
        "/first": {"items": [], "nextPagePath": "/second"},
        "/second": {"items": [], "nextPagePath": "/first"},
    }
    with pytest.raises(HistoryPaginationError, match="loop"):
        fetch_history_records(
            lambda path: pages[path],
            first_path="/first",
        )


def test_max_pages_fails_closed():
    pages = {
        "/1": {"items": [], "nextPagePath": "/2"},
        "/2": {"items": [], "nextPagePath": "/3"},
        "/3": {"items": [], "nextPagePath": None},
    }
    with pytest.raises(HistoryPaginationError, match="max_pages"):
        fetch_history_records(
            lambda path: pages[path],
            first_path="/1",
            max_pages=2,
        )
