from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentIdentity:
    ticker: str
    short_name: str
    name: str
    isin: str
    currency: str
    instrument_type: str


def _identity(item: dict) -> InstrumentIdentity:
    return InstrumentIdentity(
        ticker=str(item.get("ticker", "")),
        short_name=str(item.get("shortName", "")),
        name=str(item.get("name", "")),
        isin=str(item.get("isin", "")),
        currency=str(item.get("currencyCode", "")),
        instrument_type=str(item.get("type", "")),
    )


def resolve_us_stock(items: list[dict], symbol: str) -> InstrumentIdentity:
    symbol = symbol.strip().upper()
    matches = [
        _identity(i)
        for i in items
        if str(i.get("shortName", "")).upper() == symbol
        and str(i.get("type", "")).upper() == "STOCK"
        and str(i.get("currencyCode", "")).upper() == "USD"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one USD STOCK for {symbol}; got {len(matches)}: {matches}"
        )
    return matches[0]


def resolve_vuaa_eur(items: list[dict]) -> InstrumentIdentity:
    matches = [
        _identity(i)
        for i in items
        if str(i.get("isin", "")).upper() == "IE00BFMXXD54"
        and str(i.get("type", "")).upper() == "ETF"
        and str(i.get("currencyCode", "")).upper() == "EUR"
        and str(i.get("shortName", "")).upper() == "VUAA"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one EUR VUAA Acc instrument; got {len(matches)}: {matches}"
        )
    return matches[0]
