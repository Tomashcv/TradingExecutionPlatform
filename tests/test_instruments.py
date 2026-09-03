import pytest

from sp1execution.broker.instruments import resolve_us_stock, resolve_vuaa_eur

ITEMS = [
    {
        "ticker": "NVDA_US_EQ",
        "shortName": "NVDA",
        "name": "NVIDIA",
        "isin": "US67066G1040",
        "currencyCode": "USD",
        "type": "STOCK",
    },
    {
        "ticker": "NV3Sl_EQ",
        "shortName": "NV3S",
        "name": "Leverage Shares -3x Short Nvidia",
        "isin": "XS2944874416",
        "currencyCode": "USD",
        "type": "ETF",
    },
    {
        "ticker": "VUAAm_EQ",
        "shortName": "VUAA",
        "name": "Vanguard S&P 500 (Acc)",
        "isin": "IE00BFMXXD54",
        "currencyCode": "EUR",
        "type": "ETF",
    },
]


def test_exact_stock_resolution_ignores_leveraged_products():
    x = resolve_us_stock(ITEMS, "NVDA")
    assert x.ticker == "NVDA_US_EQ"
    assert x.isin == "US67066G1040"


def test_exact_vuaa_eur():
    x = resolve_vuaa_eur(ITEMS)
    assert x.ticker == "VUAAm_EQ"


def test_missing_stock_fails_closed():
    with pytest.raises(RuntimeError):
        resolve_us_stock(ITEMS, "AAPL")
