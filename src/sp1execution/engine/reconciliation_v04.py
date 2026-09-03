from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sp1execution.broker.history_v04 import (
    HistoryPaginationError,
    HistorySchemaError,
    assert_expected_order,
    canonical_state,
    fetch_history_records,
)
from sp1execution.engine.reconciliation import classify_order


@dataclass(frozen=True)
class ReconciledOrder:
    broker_order_id: str
    ticker: str
    expected_quantity: float
    state: str
    filled_quantity: float | None
    broker_status: str | None
    evidence_source: str


def _pending_by_id(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, list):
        raise TypeError("Trading212 pending-orders response must be a list.")

    out: dict[str, dict[str, Any]] = {}

    for row in payload:
        if not isinstance(row, dict):
            raise TypeError("Trading212 pending order must be an object.")

        raw_id = row.get("id")
        if raw_id is None:
            continue

        oid = str(raw_id)

        if oid in out and out[oid] != row:
            raise HistorySchemaError(f"conflicting pending payloads for broker order {oid}")

        out[oid] = row

    return out


def reconcile_accepted_attempts(
    *,
    attempts: list[dict[str, Any]],
    pending_payload: Any,
    fetch_history_page: Callable[[str], dict[str, Any]],
    history_first_path: str = "/equity/history/orders?limit=50",
    history_max_pages: int = 20,
) -> list[ReconciledOrder]:
    pending_by_id = _pending_by_id(pending_payload)

    history = fetch_history_records(
        fetch_history_page,
        first_path=history_first_path,
        max_pages=history_max_pages,
    )

    results: list[ReconciledOrder] = []

    for attempt in attempts:
        broker_order_id = attempt.get("broker_order_id")

        if not broker_order_id:
            results.append(
                ReconciledOrder(
                    broker_order_id="",
                    ticker=str(attempt.get("ticker", "")),
                    expected_quantity=float(attempt.get("quantity", 0.0)),
                    state="UNKNOWN",
                    filled_quantity=None,
                    broker_status=None,
                    evidence_source="MISSING_BROKER_ORDER_ID",
                )
            )
            continue

        oid = str(broker_order_id)
        ticker = str(attempt["ticker"])
        quantity = float(attempt["quantity"])

        historical = history.get(oid)

        if historical is not None:
            assert_expected_order(
                historical,
                ticker=ticker,
                quantity=quantity,
            )

            results.append(
                ReconciledOrder(
                    broker_order_id=oid,
                    ticker=ticker,
                    expected_quantity=quantity,
                    state=canonical_state(
                        historical,
                        expected_quantity=quantity,
                    ),
                    filled_quantity=historical.filled_quantity,
                    broker_status=historical.status,
                    evidence_source="HISTORICAL_NESTED_ORDER_FILL",
                )
            )
            continue

        pending = pending_by_id.get(oid)

        if pending is not None:
            legacy = classify_order(
                broker_order_id=oid,
                ticker=ticker,
                expected_quantity=quantity,
                pending_order=pending,
                historical_order=None,
            )

            results.append(
                ReconciledOrder(
                    broker_order_id=oid,
                    ticker=ticker,
                    expected_quantity=quantity,
                    state=legacy.state,
                    filled_quantity=legacy.filled_quantity,
                    broker_status=legacy.broker_status,
                    evidence_source="PENDING_ORDER",
                )
            )
            continue

        results.append(
            ReconciledOrder(
                broker_order_id=oid,
                ticker=ticker,
                expected_quantity=quantity,
                state="UNKNOWN",
                filled_quantity=None,
                broker_status=None,
                evidence_source="ABSENT_FROM_PENDING_AND_HISTORY",
            )
        )

    return results


__all__ = [
    "HistoryPaginationError",
    "HistorySchemaError",
    "ReconciledOrder",
    "reconcile_accepted_attempts",
]
