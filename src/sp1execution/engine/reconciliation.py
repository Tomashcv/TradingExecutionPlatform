from __future__ import annotations

from dataclasses import dataclass

TERMINAL_FAILURES = {"CANCELLED", "REJECTED", "EXPIRED"}


@dataclass(frozen=True)
class ReconciledOrder:
    broker_order_id: str
    ticker: str
    state: str
    expected_quantity: float
    filled_quantity: float | None
    broker_status: str | None


def classify_order(
    *,
    broker_order_id: str,
    ticker: str,
    expected_quantity: float,
    pending_order: dict | None,
    historical_order: dict | None,
) -> ReconciledOrder:
    expected_abs = abs(float(expected_quantity))

    if pending_order is not None:
        return ReconciledOrder(
            broker_order_id=broker_order_id,
            ticker=ticker,
            state="PENDING",
            expected_quantity=expected_abs,
            filled_quantity=_number_or_none(pending_order.get("filledQuantity")),
            broker_status=_status_or_none(pending_order),
        )

    if historical_order is None:
        return ReconciledOrder(
            broker_order_id=broker_order_id,
            ticker=ticker,
            state="UNKNOWN",
            expected_quantity=expected_abs,
            filled_quantity=None,
            broker_status=None,
        )

    filled = _number_or_none(historical_order.get("filledQuantity"))
    status = _status_or_none(historical_order)

    if filled is not None and expected_abs > 0:
        tolerance = max(1e-8, expected_abs * 1e-6)
        if filled + tolerance >= expected_abs:
            return ReconciledOrder(
                broker_order_id=broker_order_id,
                ticker=ticker,
                state="FILLED",
                expected_quantity=expected_abs,
                filled_quantity=filled,
                broker_status=status,
            )
        if filled > tolerance:
            return ReconciledOrder(
                broker_order_id=broker_order_id,
                ticker=ticker,
                state="PARTIAL",
                expected_quantity=expected_abs,
                filled_quantity=filled,
                broker_status=status,
            )

    if status in TERMINAL_FAILURES:
        state = "FAILED"
    else:
        state = "UNKNOWN"

    return ReconciledOrder(
        broker_order_id=broker_order_id,
        ticker=ticker,
        state=state,
        expected_quantity=expected_abs,
        filled_quantity=filled,
        broker_status=status,
    )


def _number_or_none(value) -> float | None:
    if isinstance(value, (int, float)):
        return abs(float(value))
    return None


def _status_or_none(order: dict) -> str | None:
    value = order.get("status")
    if value is None:
        return None
    return str(value).upper()
