from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from sp1execution.market_data.yahoo_chart import (
    DailyClose,
    YahooChartProvider,
    parse_daily_chart_result,
)


def _ts(date_text: str) -> int:
    return int(
        datetime.fromisoformat(date_text).replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
    )


def _valid_result(count: int = 120) -> dict:
    start = datetime(2026, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))

    timestamps = []
    closes = []
    dt = start

    while len(timestamps) < count:
        if dt.weekday() < 5:
            timestamps.append(int(dt.timestamp()))
            closes.append(100.0 + len(timestamps))
        dt = dt.replace(hour=9, minute=30) + __import__("datetime").timedelta(days=1)

    return {
        "meta": {
            "dataGranularity": "1d",
            "exchangeTimezoneName": "America/New_York",
        },
        "timestamp": timestamps,
        "indicators": {
            "quote": [
                {
                    "close": closes,
                }
            ]
        },
    }


def test_period_request_uses_periods_not_range(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    provider = YahooChartProvider()
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return {
            "chart": {
                "error": None,
                "result": [_valid_result()],
            }
        }

    monkeypatch.setattr(provider, "_get_json", fake_get)

    result = provider._chart_period(
        "IVV",
        period1=946684800,
        period2=1786644000,
        interval="1d",
    )

    assert result["meta"]["dataGranularity"] == "1d"

    parsed = urlparse(captured["url"])
    params = parse_qs(parsed.query)

    assert "range" not in params
    assert params["period1"] == ["946684800"]
    assert params["period2"] == ["1786644000"]
    assert params["interval"] == ["1d"]


def test_monthly_granularity_fails_closed():
    result = _valid_result()
    result["meta"]["dataGranularity"] = "1mo"

    with pytest.raises(RuntimeError, match="granularity mismatch"):
        parse_daily_chart_result(result, symbol="IVV")


def test_wrong_exchange_timezone_fails_closed():
    result = _valid_result()
    result["meta"]["exchangeTimezoneName"] = "UTC"

    with pytest.raises(RuntimeError, match="timezone mismatch"):
        parse_daily_chart_result(result, symbol="IVV")


def test_duplicate_daily_timestamp_fails_closed():
    result = _valid_result()
    result["timestamp"][1] = result["timestamp"][0]

    with pytest.raises(RuntimeError, match="not monotonic|Duplicate"):
        parse_daily_chart_result(result, symbol="IVV")


def test_weekend_daily_session_fails_closed():
    result = _valid_result()
    result["timestamp"][0] = int(
        datetime(
            2026,
            1,
            3,
            9,
            30,
            tzinfo=ZoneInfo("America/New_York"),
        ).timestamp()
    )
    result["timestamp"].sort()

    # Keep closes aligned in count; the validation is about the session date.
    with pytest.raises(RuntimeError, match="Weekend"):
        parse_daily_chart_result(result, symbol="IVV")


def test_daily_close_date_uses_new_york_session_date():
    row = DailyClose(
        timestamp=int(
            datetime(
                2026,
                8,
                13,
                9,
                30,
                tzinfo=ZoneInfo("America/New_York"),
            ).timestamp()
        ),
        close=100.0,
    )

    assert row.date == "2026-08-13"


def test_start_date_epoch_is_stable_via_request(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    provider = YahooChartProvider()
    captured = {}

    def fake_chart_period(symbol, *, period1, period2, interval):
        captured.update(
            {
                "symbol": symbol,
                "period1": period1,
                "period2": period2,
                "interval": interval,
            }
        )
        return _valid_result()

    monkeypatch.setattr(provider, "_chart_period", fake_chart_period)

    provider.daily_history(
        "IVV",
        start_date="2000-01-01",
        now_ts=1786644000,
    )

    expected = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp())

    assert captured["period1"] == expected
    assert captured["period2"] == 1786644000
    assert captured["interval"] == "1d"


def test_legacy_max_argument_never_reaches_range_chart(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    provider = YahooChartProvider()

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy range chart must not be used for daily history")

    monkeypatch.setattr(provider, "_chart", forbidden)
    monkeypatch.setattr(
        provider,
        "_chart_period",
        lambda *args, **kwargs: _valid_result(),
    )

    rows = provider.daily_history(
        "IVV",
        "max",
        now_ts=1786644000,
    )

    assert len(rows) == 120
