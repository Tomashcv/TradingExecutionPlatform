from datetime import datetime
from zoneinfo import ZoneInfo

from sp1execution.market_data.yahoo_chart import DailyClose, completed_daily_rows


def _ts(date_text: str) -> int:
    return int(
        datetime.fromisoformat(date_text)
        .replace(tzinfo=ZoneInfo("UTC"))
        .timestamp()
    )


def test_intraday_current_daily_bar_is_excluded():
    rows = [
        DailyClose(_ts("2026-08-12T20:00:00"), 100.0),
        DailyClose(_ts("2026-08-13T20:00:00"), 101.0),
    ]
    now_ny = datetime(2026, 8, 13, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    out = completed_daily_rows(rows, now_ny=now_ny)
    assert [row.close for row in out] == [100.0]


def test_current_daily_bar_allowed_after_close_grace():
    rows = [
        DailyClose(_ts("2026-08-12T20:00:00"), 100.0),
        DailyClose(_ts("2026-08-13T20:00:00"), 101.0),
    ]
    now_ny = datetime(2026, 8, 13, 16, 20, tzinfo=ZoneInfo("America/New_York"))
    out = completed_daily_rows(rows, now_ny=now_ny)
    assert [row.close for row in out] == [100.0, 101.0]
