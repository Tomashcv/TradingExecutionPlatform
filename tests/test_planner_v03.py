from sp1execution.engine.planner import (
    InstrumentQuote,
    make_orders,
)


def test_initial_fractional_half_half_plan():
    quotes = {
        "AAPL": InstrumentQuote("AAPL", "AAPL_US_EQ", 200.0, "USD"),
        "NVDA": InstrumentQuote("NVDA", "NVDA_US_EQ", 100.0, "USD"),
        "VUAA": InstrumentQuote("VUAA", "VUAAm_EQ", 100.0, "EUR"),
    }
    orders, values = make_orders(
        nav_eur=10000.0,
        target_weights={"AAPL": 0.5, "NVDA": 0.5, "VUAA": 0.0},
        quotes=quotes,
        positions=[],
        eurusd=1.2,
    )
    assert values == {"AAPL": 0.0, "NVDA": 0.0, "VUAA": 0.0}
    assert len(orders) == 2
    assert all(order.side == "BUY" for order in orders)
    assert sum(order.estimated_notional_eur for order in orders) < 10000.0


def test_sell_orders_sort_before_buys():
    quotes = {
        "AAPL": InstrumentQuote("AAPL", "AAPL_US_EQ", 100.0, "USD"),
        "NVDA": InstrumentQuote("NVDA", "NVDA_US_EQ", 100.0, "USD"),
        "VUAA": InstrumentQuote("VUAA", "VUAAm_EQ", 100.0, "EUR"),
    }
    positions = [
        {"ticker": "AAPL_US_EQ", "quantity": 100.0},
    ]
    orders, _ = make_orders(
        nav_eur=10000.0,
        target_weights={"AAPL": 0.0, "NVDA": 0.0, "VUAA": 1.0},
        quotes=quotes,
        positions=positions,
        eurusd=1.0,
    )
    assert orders[0].side == "SELL"
    assert orders[-1].side == "BUY"
