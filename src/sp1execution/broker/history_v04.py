from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class HistorySchemaError(RuntimeError):
    pass


class HistoryPaginationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanonicalFee:
    name: str
    currency: str
    amount: float
    charged_at: str | None


@dataclass(frozen=True)
class CanonicalFill:
    fill_id: str
    filled_at: str
    price: float
    quantity: float
    wallet_currency: str | None
    wallet_net_value: float | None
    fx_rate: float | None
    fees: tuple[CanonicalFee, ...]


@dataclass(frozen=True)
class CanonicalHistoricalOrder:
    broker_order_id: str
    ticker: str
    side: str
    status: str
    ordered_quantity: float
    filled_quantity: float
    created_at: str
    fills: tuple[CanonicalFill, ...]

    @property
    def observed_fill_quantity(self) -> float:
        return sum(abs(fill.quantity) for fill in self.fills)


FetchPage = Callable[[str], dict[str, Any]]


def _dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HistorySchemaError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistorySchemaError(f"{field} must be a list")
    return value


def _text(value: Any, field: str) -> str:
    if value is None or not str(value).strip():
        raise HistorySchemaError(f"{field} is required")
    return str(value).strip()


def _float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HistorySchemaError(f"{field} must be numeric") from exc


def _opt_float(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _float(value, field)


def parse_fee(raw: Any) -> CanonicalFee:
    row = _dict(raw, "fill.walletImpact.taxes[]")
    return CanonicalFee(
        name=_text(row.get("name"), "fee.name"),
        currency=_text(row.get("currency"), "fee.currency"),
        amount=_float(row.get("quantity"), "fee.quantity"),
        charged_at=None if row.get("chargedAt") is None else str(row["chargedAt"]),
    )


def parse_fill(raw: Any) -> CanonicalFill:
    row = _dict(raw, "fill")
    wallet_raw = row.get("walletImpact")

    if wallet_raw is None:
        wallet = None
        fees: tuple[CanonicalFee, ...] = ()
    else:
        wallet = _dict(wallet_raw, "fill.walletImpact")
        taxes_raw = wallet.get("taxes", [])
        if taxes_raw is None:
            taxes_raw = []
        fees = tuple(parse_fee(item) for item in _list(taxes_raw, "fill.walletImpact.taxes"))

    return CanonicalFill(
        fill_id=_text(row.get("id"), "fill.id"),
        filled_at=_text(row.get("filledAt"), "fill.filledAt"),
        price=_float(row.get("price"), "fill.price"),
        quantity=_float(row.get("quantity"), "fill.quantity"),
        wallet_currency=(
            None if wallet is None or wallet.get("currency") is None else str(wallet["currency"])
        ),
        wallet_net_value=(
            None
            if wallet is None
            else _opt_float(wallet.get("netValue"), "fill.walletImpact.netValue")
        ),
        fx_rate=(
            None if wallet is None else _opt_float(wallet.get("fxRate"), "fill.walletImpact.fxRate")
        ),
        fees=fees,
    )


def parse_history_item(raw: Any) -> CanonicalHistoricalOrder:
    item = _dict(raw, "history.items[]")
    order = _dict(item.get("order"), "order")
    fill_raw = item.get("fill")
    fills = () if fill_raw is None else (parse_fill(fill_raw),)

    return CanonicalHistoricalOrder(
        broker_order_id=_text(order.get("id"), "order.id"),
        ticker=_text(order.get("ticker"), "order.ticker"),
        side=_text(order.get("side"), "order.side").upper(),
        status=_text(order.get("status"), "order.status").upper(),
        ordered_quantity=_float(order.get("quantity"), "order.quantity"),
        filled_quantity=_float(order.get("filledQuantity", 0.0), "order.filledQuantity"),
        created_at=_text(order.get("createdAt"), "order.createdAt"),
        fills=fills,
    )


def _same_identity(
    left: CanonicalHistoricalOrder,
    right: CanonicalHistoricalOrder,
) -> bool:
    return (
        left.broker_order_id == right.broker_order_id
        and left.ticker == right.ticker
        and left.side == right.side
        and abs(abs(left.ordered_quantity) - abs(right.ordered_quantity)) <= 1e-12
        and left.created_at == right.created_at
    )


def merge_records(
    left: CanonicalHistoricalOrder,
    right: CanonicalHistoricalOrder,
) -> CanonicalHistoricalOrder:
    if not _same_identity(left, right):
        raise HistorySchemaError("conflicting snapshots for same broker order ID")

    fills_by_id: dict[str, CanonicalFill] = {}
    for fill in left.fills + right.fills:
        prior = fills_by_id.get(fill.fill_id)
        if prior is not None and prior != fill:
            raise HistorySchemaError("conflicting payloads for same fill ID")
        fills_by_id[fill.fill_id] = fill

    winner = left if abs(left.filled_quantity) > abs(right.filled_quantity) else right

    return CanonicalHistoricalOrder(
        broker_order_id=winner.broker_order_id,
        ticker=winner.ticker,
        side=winner.side,
        status=winner.status,
        ordered_quantity=winner.ordered_quantity,
        filled_quantity=winner.filled_quantity,
        created_at=winner.created_at,
        fills=tuple(
            sorted(
                fills_by_id.values(),
                key=lambda fill: (fill.filled_at, fill.fill_id),
            )
        ),
    )


def parse_history_page(
    payload: Any,
) -> tuple[dict[str, CanonicalHistoricalOrder], str | None]:
    page = _dict(payload, "history")
    items = _list(page.get("items"), "history.items")
    records: dict[str, CanonicalHistoricalOrder] = {}

    for raw in items:
        record = parse_history_item(raw)
        oid = record.broker_order_id
        if oid in records:
            records[oid] = merge_records(records[oid], record)
        else:
            records[oid] = record

    next_page = page.get("nextPagePath")
    if next_page is not None:
        next_page = _text(next_page, "history.nextPagePath")

    return records, next_page


def fetch_history_records(
    fetch_page: FetchPage,
    *,
    first_path: str = "/equity/history/orders?limit=50",
    max_pages: int = 20,
) -> dict[str, CanonicalHistoricalOrder]:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")

    path: str | None = first_path
    visited: set[str] = set()
    out: dict[str, CanonicalHistoricalOrder] = {}
    pages = 0

    while path is not None:
        if path in visited:
            raise HistoryPaginationError(f"pagination loop at {path}")
        if pages >= max_pages:
            raise HistoryPaginationError(f"exceeded max_pages={max_pages}")

        visited.add(path)
        page_records, next_page = parse_history_page(fetch_page(path))

        for oid, record in page_records.items():
            if oid in out:
                out[oid] = merge_records(out[oid], record)
            else:
                out[oid] = record

        path = next_page
        pages += 1

    return out


def fetch_exact_history_order(
    fetch_page: FetchPage,
    *,
    broker_order_id: str,
    first_path: str = "/equity/history/orders?limit=50",
    max_pages: int = 20,
) -> CanonicalHistoricalOrder | None:
    return fetch_history_records(
        fetch_page,
        first_path=first_path,
        max_pages=max_pages,
    ).get(str(broker_order_id))


def assert_expected_order(
    record: CanonicalHistoricalOrder,
    *,
    ticker: str,
    quantity: float,
    tolerance: float = 1e-8,
) -> None:
    if record.ticker != ticker:
        raise HistorySchemaError(f"ticker mismatch: broker={record.ticker} expected={ticker}")
    if abs(abs(record.ordered_quantity) - abs(float(quantity))) > tolerance:
        raise HistorySchemaError(
            f"quantity mismatch: broker={record.ordered_quantity} expected={quantity}"
        )


def canonical_state(
    record: CanonicalHistoricalOrder,
    *,
    expected_quantity: float | None = None,
    tolerance: float = 1e-8,
) -> str:
    status = record.status.upper()
    ordered = abs(
        float(record.ordered_quantity if expected_quantity is None else expected_quantity)
    )
    filled = abs(float(record.filled_quantity))

    if status == "FILLED":
        if abs(filled - ordered) <= tolerance:
            return "FILLED"
        return "PARTIAL" if filled > tolerance else "UNKNOWN"

    if "PARTIAL" in status or (filled > tolerance and filled < ordered - tolerance):
        return "PARTIAL"

    if status in {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}:
        return "PARTIAL" if filled > tolerance else "FAILED"

    if status in {"NEW", "PENDING", "OPEN", "ACCEPTED", "WORKING"}:
        return "PENDING"

    return "UNKNOWN"
