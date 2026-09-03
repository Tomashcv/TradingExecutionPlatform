"""
A5C Trading 212 Demo read-model adapter.

Pure normalization / validation only.

No network.
No database.
No broker POST.
No order creation.
No live authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping


ACCOUNT_PRIMARY_CURRENCY = "EUR"

RECOVERY_ISIN = "IE00BMC38736"
RECOVERY_TICKER = "SMHm_EQ"
RECOVERY_CURRENCY = "EUR"

RESEARCH_PROXY = "SOXX"

LIVE_EXECUTION_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False


class DemoAdapterError(ValueError):
    pass


def _d(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise DemoAdapterError(
            f"{field}: invalid decimal"
        ) from exc

    if not result.is_finite():
        raise DemoAdapterError(
            f"{field}: non-finite decimal"
        )

    return result


@dataclass(frozen=True)
class DemoAccountSnapshot:
    currency: str
    available_to_trade_eur: Decimal
    reserved_for_orders_eur: Decimal
    in_pies_eur: Decimal

    broker_post_authorized: bool = False
    live_execution_authorized: bool = False


@dataclass(frozen=True)
class DemoInstrument:
    ticker: str
    isin: str
    currency: str
    instrument_type: str
    name: str


@dataclass(frozen=True)
class DemoPosition:
    ticker: str
    isin: str

    quantity: Decimal
    quantity_available_for_trading: Decimal
    quantity_in_pies: Decimal

    current_price: Decimal | None
    average_price_paid: Decimal | None

    wallet_impact: Mapping[str, Any] | None

    @property
    def fully_available_for_trading(self) -> bool:
        return (
            abs(
                self.quantity
                -
                self.quantity_available_for_trading
            )
            <= Decimal("0.00000001")
            and
            abs(self.quantity_in_pies)
            <= Decimal("0.00000001")
        )


def normalize_account_summary(
    payload: Mapping[str, Any],
) -> DemoAccountSnapshot:

    if not isinstance(payload, Mapping):
        raise DemoAdapterError(
            "account summary must be a mapping"
        )

    currency = str(
        payload.get("currency", "")
    ).upper()

    if currency != ACCOUNT_PRIMARY_CURRENCY:
        raise DemoAdapterError(
            f"expected account currency EUR; got {currency!r}"
        )

    cash = payload.get("cash")

    if not isinstance(cash, Mapping):
        raise DemoAdapterError(
            "account cash object missing"
        )

    available = _d(
        cash.get("availableToTrade"),
        "cash.availableToTrade",
    )

    reserved = _d(
        cash.get("reservedForOrders", 0),
        "cash.reservedForOrders",
    )

    in_pies = _d(
        cash.get("inPies", 0),
        "cash.inPies",
    )

    if min(
        available,
        reserved,
        in_pies,
    ) < 0:
        raise DemoAdapterError(
            "negative cash field"
        )

    return DemoAccountSnapshot(
        currency=currency,
        available_to_trade_eur=available,
        reserved_for_orders_eur=reserved,
        in_pies_eur=in_pies,
    )


def resolve_recovery_instrument(
    rows: Iterable[Mapping[str, Any]],
) -> DemoInstrument:

    exact_isin = []

    for row in rows:

        if not isinstance(row, Mapping):
            continue

        if (
            str(
                row.get("isin", "")
            ).strip()
            ==
            RECOVERY_ISIN
        ):
            exact_isin.append(row)

    eur = [
        row
        for row in exact_isin
        if (
            str(
                row.get("currencyCode", "")
            ).upper()
            ==
            ACCOUNT_PRIMARY_CURRENCY
        )
    ]

    if len(exact_isin) != 3:
        raise DemoAdapterError(
            "expected 3 exact recovery ISIN listings "
            f"from frozen Demo discovery; got {len(exact_isin)}"
        )

    if len(eur) != 1:
        raise DemoAdapterError(
            "recovery ISIN must have exactly one EUR listing"
        )

    row = eur[0]

    ticker = str(
        row.get("ticker", "")
    )

    if ticker != RECOVERY_TICKER:
        raise DemoAdapterError(
            f"expected recovery ticker {RECOVERY_TICKER}; "
            f"got {ticker!r}"
        )

    instrument_type = str(
        row.get("type", "")
    ).upper()

    if instrument_type != "ETF":
        raise DemoAdapterError(
            "recovery instrument is not ETF"
        )

    return DemoInstrument(
        ticker=ticker,
        isin=RECOVERY_ISIN,
        currency=ACCOUNT_PRIMARY_CURRENCY,
        instrument_type=instrument_type,
        name=str(
            row.get("name", "")
        ),
    )


def normalize_position(
    row: Mapping[str, Any],
) -> DemoPosition:

    if not isinstance(row, Mapping):
        raise DemoAdapterError(
            "position must be mapping"
        )

    instrument = row.get("instrument")

    if not isinstance(instrument, Mapping):
        raise DemoAdapterError(
            "position instrument missing"
        )

    ticker = str(
        instrument.get("ticker", "")
    )

    isin = str(
        instrument.get("isin", "")
    )

    if not ticker:
        raise DemoAdapterError(
            "position ticker missing"
        )

    if not isin:
        raise DemoAdapterError(
            "position ISIN missing"
        )

    quantity = _d(
        row.get("quantity"),
        "quantity",
    )

    available = _d(
        row.get("quantityAvailableForTrading"),
        "quantityAvailableForTrading",
    )

    in_pies = _d(
        row.get("quantityInPies", 0),
        "quantityInPies",
    )

    if (
        quantity < 0
        or
        available < 0
        or
        in_pies < 0
    ):
        raise DemoAdapterError(
            "negative position quantity"
        )

    if available > quantity:
        raise DemoAdapterError(
            "available quantity exceeds total quantity"
        )

    current_price_raw = row.get(
        "currentPrice"
    )

    average_price_raw = row.get(
        "averagePricePaid"
    )

    current_price = (
        None
        if current_price_raw is None
        else _d(
            current_price_raw,
            "currentPrice",
        )
    )

    average_price = (
        None
        if average_price_raw is None
        else _d(
            average_price_raw,
            "averagePricePaid",
        )
    )

    wallet = row.get(
        "walletImpact"
    )

    if wallet is not None and not isinstance(
        wallet,
        Mapping,
    ):
        raise DemoAdapterError(
            "walletImpact must be mapping or null"
        )

    return DemoPosition(
        ticker=ticker,
        isin=isin,
        quantity=quantity,
        quantity_available_for_trading=available,
        quantity_in_pies=in_pies,
        current_price=current_price,
        average_price_paid=average_price,
        wallet_impact=wallet,
    )


def normalize_positions(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[DemoPosition, ...]:

    positions = tuple(
        normalize_position(row)
        for row in rows
    )

    tickers = [
        position.ticker
        for position in positions
    ]

    if len(tickers) != len(set(tickers)):
        raise DemoAdapterError(
            "duplicate ticker position"
        )

    return tuple(
        sorted(
            positions,
            key=lambda p: p.ticker,
        )
    )


def require_demo_strategy_inventory(
    positions: Iterable[DemoPosition],
) -> dict[str, DemoPosition]:

    by_ticker = {
        p.ticker: p
        for p in positions
    }

    allowed = {
        "AAPL_US_EQ",
        "NVDA_US_EQ",
        RECOVERY_TICKER,
    }

    unrelated = (
        set(by_ticker)
        -
        allowed
    )

    if unrelated:
        raise DemoAdapterError(
            "unrelated Demo positions present: "
            + repr(sorted(unrelated))
        )

    for ticker in [
        "AAPL_US_EQ",
        "NVDA_US_EQ",
    ]:
        p = by_ticker.get(ticker)

        if p is None:
            raise DemoAdapterError(
                f"required SP2 position missing: {ticker}"
            )

        if not p.fully_available_for_trading:
            raise DemoAdapterError(
                f"{ticker} not fully available for trading"
            )

    recovery = by_ticker.get(
        RECOVERY_TICKER
    )

    if (
        recovery is not None
        and
        not recovery.fully_available_for_trading
    ):
        raise DemoAdapterError(
            "recovery position not fully available for trading"
        )

    return by_ticker
