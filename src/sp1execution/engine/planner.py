from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentQuote:
    logical_symbol: str
    broker_ticker: str
    price: float
    currency: str


@dataclass(frozen=True)
class PlannedOrder:
    logical_symbol: str
    broker_ticker: str
    quantity: float
    side: str
    estimated_notional_eur: float
    delta_eur: float


def position_quantities(positions: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}

    for index, row in enumerate(positions):
        if not isinstance(row, dict):
            raise TypeError(f"position row {index} must be a mapping")

        flat_raw = row.get("ticker")
        instrument = row.get("instrument")
        nested_raw = instrument.get("ticker") if isinstance(instrument, dict) else None

        flat = str(flat_raw).strip() if flat_raw is not None else ""
        nested = str(nested_raw).strip() if nested_raw is not None else ""

        if flat and nested and flat != nested:
            raise ValueError(
                "position row "
                f"{index} has conflicting ticker fields: "
                f"flat={flat!r} nested={nested!r}"
            )

        ticker = nested or flat
        if not ticker:
            raise ValueError(f"position row {index} missing ticker")

        if ticker in out:
            raise ValueError(f"duplicate ticker in positions payload: {ticker}")

        raw_quantity = row.get("quantity", 0.0)
        try:
            quantity = float(raw_quantity or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"position row {index} has invalid quantity for {ticker}: {raw_quantity!r}"
            ) from exc

        if not float("-inf") < quantity < float("inf"):
            raise ValueError(
                f"position row {index} has invalid quantity for {ticker}: {raw_quantity!r}"
            )

        out[ticker] = quantity

    return out


def value_position_eur(quantity: float, quote: InstrumentQuote, eurusd: float) -> float:
    if quote.currency == "EUR":
        return quantity * quote.price
    if quote.currency == "USD":
        if eurusd <= 0:
            raise ValueError("EURUSD must be positive.")
        return quantity * quote.price / eurusd
    raise RuntimeError(f"Unsupported quote currency: {quote.currency}")


def quantity_for_delta_eur(delta_eur: float, quote: InstrumentQuote, eurusd: float) -> float:
    if quote.currency == "EUR":
        return delta_eur / quote.price
    if quote.currency == "USD":
        return delta_eur * eurusd / quote.price
    raise RuntimeError(f"Unsupported quote currency: {quote.currency}")


def make_orders(
    *,
    nav_eur: float,
    target_weights: dict[str, float],
    quotes: dict[str, InstrumentQuote],
    positions: list[dict],
    eurusd: float,
    tolerance_fraction_nav: float = 0.0025,
    buy_buffer: float = 0.9975,
) -> tuple[list[PlannedOrder], dict[str, float]]:
    if nav_eur <= 0:
        raise ValueError("NAV must be positive.")
    if not 0.95 <= sum(target_weights.values()) <= 1.0000001:
        raise ValueError(f"Unexpected target weight sum: {sum(target_weights.values())}")

    quantities = position_quantities(positions)
    current_values: dict[str, float] = {}
    orders: list[PlannedOrder] = []
    tolerance_eur = nav_eur * tolerance_fraction_nav

    for logical, quote in quotes.items():
        current_qty = quantities.get(quote.broker_ticker, 0.0)
        current_values[logical] = value_position_eur(current_qty, quote, eurusd)

    for logical, target_weight in target_weights.items():
        quote = quotes[logical]
        target_eur = nav_eur * target_weight
        delta = target_eur - current_values.get(logical, 0.0)
        if abs(delta) <= tolerance_eur:
            continue

        effective_delta = delta if delta < 0 else delta * buy_buffer
        quantity = quantity_for_delta_eur(effective_delta, quote, eurusd)

        # Fractional shares are supported; keep enough precision without
        # manufacturing microscopic orders.
        quantity = int(quantity * 10_000) / 10_000
        if abs(quantity) < 1e-8:
            continue

        orders.append(
            PlannedOrder(
                logical_symbol=logical,
                broker_ticker=quote.broker_ticker,
                quantity=quantity,
                side="BUY" if quantity > 0 else "SELL",
                estimated_notional_eur=abs(effective_delta),
                delta_eur=delta,
            )
        )

    orders.sort(key=lambda order: 0 if order.side == "SELL" else 1)
    return orders, current_values
