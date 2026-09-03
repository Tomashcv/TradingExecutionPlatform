from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DAILY_SCHEMA = "yahoo_period_daily_v04"
DEFAULT_DAILY_START = "2000-01-01"


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    currency: str
    timestamp: int

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.timestamp)


@dataclass(frozen=True)
class DailyClose:
    timestamp: int
    close: float

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=NY).date().isoformat()


def completed_daily_rows(
    rows: list[DailyClose],
    *,
    now_ny: datetime | None = None,
) -> list[DailyClose]:
    if now_ny is None:
        now_ny = datetime.now(NY)
    elif now_ny.tzinfo is None:
        raise ValueError("now_ny must be timezone-aware")
    else:
        now_ny = now_ny.astimezone(NY)

    today = now_ny.date().isoformat()
    after_close_grace = (now_ny.hour, now_ny.minute) >= (16, 15)

    if after_close_grace:
        return list(rows)

    return [row for row in rows if row.date < today]


def _start_epoch(date_text: str) -> int:
    try:
        dt = datetime.fromisoformat(date_text)
    except ValueError as exc:
        raise ValueError(f"Invalid start date: {date_text}") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    return int(dt.timestamp())


def _validate_daily_rows(
    rows: list[DailyClose],
    *,
    symbol: str,
) -> None:
    if len(rows) < 100:
        raise RuntimeError(f"Daily history unexpectedly short for {symbol}: {len(rows)}")

    timestamps = [row.timestamp for row in rows]
    if timestamps != sorted(timestamps):
        raise RuntimeError(f"Daily timestamps not monotonic for {symbol}")

    if len(set(timestamps)) != len(timestamps):
        raise RuntimeError(f"Duplicate daily timestamps for {symbol}")

    dates = [row.date for row in rows]
    if len(set(dates)) != len(dates):
        raise RuntimeError(f"Duplicate daily session dates for {symbol}")

    for row in rows:
        if row.close <= 0:
            raise RuntimeError(f"Non-positive daily close for {symbol}")

        session = datetime.fromtimestamp(row.timestamp, tz=NY)
        if session.weekday() >= 5:
            raise RuntimeError(f"Weekend daily session for {symbol}: {row.date}")


def parse_daily_chart_result(
    result: dict,
    *,
    symbol: str,
) -> list[DailyClose]:
    meta = result.get("meta") or {}

    granularity = meta.get("dataGranularity")
    if granularity != "1d":
        raise RuntimeError(
            f"Yahoo daily granularity mismatch for {symbol}: expected=1d actual={granularity!r}"
        )

    exchange_tz = meta.get("exchangeTimezoneName")
    if exchange_tz != "America/New_York":
        raise RuntimeError(
            f"Yahoo exchange timezone mismatch for {symbol}: "
            f"expected=America/New_York actual={exchange_tz!r}"
        )

    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quote_rows = indicators.get("quote") or []

    if len(quote_rows) != 1:
        raise RuntimeError(f"No daily quote array for {symbol}")

    closes = quote_rows[0].get("close") or []

    if len(timestamps) != len(closes):
        raise RuntimeError(
            f"Yahoo timestamp/close length mismatch for {symbol}: "
            f"{len(timestamps)} != {len(closes)}"
        )

    rows: list[DailyClose] = []

    for ts, close in zip(timestamps, closes, strict=True):
        if close is None:
            continue

        if not isinstance(ts, int):
            raise TypeError(f"Non-integer daily timestamp for {symbol}")

        if not isinstance(close, (int, float)):
            raise TypeError(f"Non-numeric daily close for {symbol}")

        rows.append(
            DailyClose(
                timestamp=ts,
                close=float(close),
            )
        )

    _validate_daily_rows(rows, symbol=symbol)
    return rows


class YahooChartProvider:
    # PAPER/DEMO market-data adapter. Not live-approved.
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.cache_dir = Path("state/market_data")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_json(self, url: str):
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 SP1Execution/0.4 paper-only",
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    @staticmethod
    def _result(payload: dict, *, symbol: str) -> dict:
        chart = payload.get("chart", {})

        if chart.get("error") is not None:
            raise RuntimeError(f"Yahoo chart error for {symbol}: {chart['error']}")

        results = chart.get("result") or []

        if len(results) != 1:
            raise RuntimeError(f"Yahoo chart returned {len(results)} results for {symbol}")

        return results[0]

    def _chart(self, symbol: str, *, range_: str, interval: str):
        encoded = quote(symbol, safe="")
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
            f"?range={range_}&interval={interval}&includePrePost=false"
            "&events=div%2Csplits"
        )
        return self._result(
            self._get_json(url),
            symbol=symbol,
        )

    def _chart_period(
        self,
        symbol: str,
        *,
        period1: int,
        period2: int,
        interval: str,
    ) -> dict:
        if period2 <= period1:
            raise ValueError("period2 must be greater than period1")

        encoded = quote(symbol, safe="")
        params = {
            "period1": int(period1),
            "period2": int(period2),
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?" + urlencode(params)

        return self._result(
            self._get_json(url),
            symbol=symbol,
        )

    def quote(self, symbol: str) -> Quote:
        result = self._chart(symbol, range_="1d", interval="1m")
        meta = result.get("meta") or {}

        price = meta.get("regularMarketPrice")
        ts = meta.get("regularMarketTime")
        currency = meta.get("currency")

        if not isinstance(price, (int, float)) or not isinstance(ts, int):
            raise TypeError(f"No usable regular-market quote for {symbol}")

        return Quote(
            symbol=symbol,
            price=float(price),
            currency=str(currency or ""),
            timestamp=ts,
        )

    def daily_history(
        self,
        symbol: str,
        range_: str | None = None,
        *,
        start_date: str = DEFAULT_DAILY_START,
        now_ts: int | None = None,
    ) -> list[DailyClose]:
        # `range_` is retained only as a backwards-compatible call argument.
        # It is NEVER forwarded to Yahoo for daily signal history.
        if range_ not in (None, "max"):
            raise ValueError(
                "v0.4 daily history accepts only range_=None/'max'; "
                "history is fetched causally using explicit period1/period2."
            )

        if now_ts is None:
            now_ts = int(time.time())

        period1 = _start_epoch(start_date)
        period2 = int(now_ts)

        cache_key = symbol.replace("^", "_").replace("=", "_").replace("/", "_")
        cache = self.cache_dir / f"yahoo_{cache_key}_{start_date}_period_1d_v04.json"
        ttl = 15 * 60

        if cache.exists():
            try:
                payload = json.loads(cache.read_text())

                if (
                    payload.get("schema") == DAILY_SCHEMA
                    and payload.get("start_date") == start_date
                    and payload.get("data_granularity") == "1d"
                    and payload.get("exchange_timezone") == "America/New_York"
                    and time.time() - float(payload["fetched_at"]) <= ttl
                ):
                    rows = [
                        DailyClose(
                            timestamp=int(x["timestamp"]),
                            close=float(x["close"]),
                        )
                        for x in payload["rows"]
                    ]
                    _validate_daily_rows(rows, symbol=symbol)
                    return rows

            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                json.JSONDecodeError,
            ):
                pass

        result = self._chart_period(
            symbol,
            period1=period1,
            period2=period2,
            interval="1d",
        )

        rows = parse_daily_chart_result(
            result,
            symbol=symbol,
        )

        meta = result.get("meta") or {}

        cache.write_text(
            json.dumps(
                {
                    "schema": DAILY_SCHEMA,
                    "fetched_at": time.time(),
                    "start_date": start_date,
                    "period1": period1,
                    "period2": period2,
                    "data_granularity": meta.get("dataGranularity"),
                    "exchange_timezone": meta.get("exchangeTimezoneName"),
                    "rows": [
                        {
                            "timestamp": row.timestamp,
                            "close": row.close,
                        }
                        for row in rows
                    ],
                },
                separators=(",", ":"),
            )
        )

        return rows

    def completed_daily_history(
        self,
        symbol: str,
        range_: str | None = None,
        *,
        start_date: str = DEFAULT_DAILY_START,
        now_ny: datetime | None = None,
        now_ts: int | None = None,
    ) -> list[DailyClose]:
        rows = completed_daily_rows(
            self.daily_history(
                symbol,
                range_,
                start_date=start_date,
                now_ts=now_ts,
            ),
            now_ny=now_ny,
        )

        if not rows:
            raise RuntimeError(f"No completed daily rows for {symbol}")

        return rows
