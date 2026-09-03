from sp1execution.engine.reconciliation import classify_order


def test_pending_order_stays_pending():
    out = classify_order(
        broker_order_id="1",
        ticker="AAPL_US_EQ",
        expected_quantity=2.0,
        pending_order={"id": 1, "filledQuantity": 0.0, "status": "NEW"},
        historical_order=None,
    )
    assert out.state == "PENDING"


def test_full_historical_fill_is_filled():
    out = classify_order(
        broker_order_id="2",
        ticker="NVDA_US_EQ",
        expected_quantity=2.0,
        pending_order=None,
        historical_order={"id": 2, "filledQuantity": 2.0, "status": "FILLED"},
    )
    assert out.state == "FILLED"


def test_partial_historical_fill_is_partial():
    out = classify_order(
        broker_order_id="3",
        ticker="NVDA_US_EQ",
        expected_quantity=2.0,
        pending_order=None,
        historical_order={"id": 3, "filledQuantity": 1.0, "status": "FILLED"},
    )
    assert out.state == "PARTIAL"


def test_cancelled_zero_fill_is_failed():
    out = classify_order(
        broker_order_id="4",
        ticker="AAPL_US_EQ",
        expected_quantity=2.0,
        pending_order=None,
        historical_order={"id": 4, "filledQuantity": 0.0, "status": "CANCELLED"},
    )
    assert out.state == "FAILED"


def test_missing_from_pending_and_history_is_unknown():
    out = classify_order(
        broker_order_id="5",
        ticker="AAPL_US_EQ",
        expected_quantity=2.0,
        pending_order=None,
        historical_order=None,
    )
    assert out.state == "UNKNOWN"
