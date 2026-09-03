from __future__ import annotations

from datetime import (
    date,
    datetime,
    time,
)
from pathlib import Path
from zoneinfo import ZoneInfo

import csv
import io
import json

import pytest


from sp1execution.recovery.current_ivv_total_return_provider_v01 import (
    ABSOLUTE_YAHOO_ADJ_CLOSE_LEVEL_AUTHORITY,
    BROKER_GET_REQUIRED,
    BROKER_POST_AUTHORIZED,
    CORE_RETURN_RULE_ID,
    FROZEN_ANCHOR_SESSION,
    FROZEN_CANONICAL_SHA256,
    FROZEN_EXPECTED_ROW_COUNT,
    LIVE_EXECUTION_AUTHORIZED,
    NETWORK_PERFORMED_BY_PROVIDER,
    SOURCE_PROVIDER_ID,
    TOTAL_RETURN_GEOMETRY_AUTHORITY,
    TRANSPORT_SANITY_CAP_BP,
    CurrentIVVProviderError,
    build_current_ivv_total_return_surface,
)


ROOT = Path(
    __file__
).resolve().parents[1]

CANON = (
    ROOT
    /
    "contracts/research/"
    "phase_b0_canonical_sp2_ivv_path_v0.1.csv"
)


def canonical_raw():
    return CANON.read_bytes()


def canonical_rows():
    raw = canonical_raw().decode(
        "utf-8"
    )

    reader = csv.DictReader(
        io.StringIO(
            raw
        )
    )

    return list(
        reader
    )


def _timestamp_for_session(
    session: str,
) -> int:

    ny = ZoneInfo(
        "America/New_York"
    )

    d = date.fromisoformat(
        session
    )

    dt = datetime.combine(
        d,
        time(
            12,
            0,
        ),
        tzinfo=ny,
    )

    return int(
        dt.timestamp()
    )


def make_yahoo_payload(
    *,
    symbol="IVV",
    currency="USD",
    timezone_name="America/New_York",
    granularity="1d",
    drop_session=None,
    tamper_session=None,
    tamper_multiplier=1.0,
):
    rows = canonical_rows()

    sessions = []
    values = []

    scale = 100.0

    for row in rows:

        session = str(
            row["date"]
        )

        if session == drop_session:
            continue

        value = (
            float(
                row["ivv_nav"]
            )
            *
            scale
        )

        if session == tamper_session:
            value *= tamper_multiplier

        sessions.append(
            session
        )

        values.append(
            value
        )

    anchor_value = (
        float(
            rows[-1][
                "ivv_nav"
            ]
        )
        *
        scale
    )

    # Three current continuation sessions.
    sessions.extend([
        "2024-11-04",
        "2024-11-05",
        "2024-11-06",
    ])

    values.extend([
        anchor_value
        *
        1.01,

        anchor_value
        *
        1.02,

        anchor_value
        *
        1.03,
    ])

    timestamps = [
        _timestamp_for_session(
            session
        )
        for session in sessions
    ]

    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol":
                            symbol,

                        "currency":
                            currency,

                        "exchangeTimezoneName":
                            timezone_name,

                        "dataGranularity":
                            granularity,

                        "instrumentType":
                            "ETF",
                    },

                    "timestamp":
                        timestamps,

                    "indicators": {
                        "adjclose": [
                            {
                                "adjclose":
                                    values
                            }
                        ]
                    },
                }
            ],

            "error":
                None,
        }
    }

    return json.dumps(
        payload,
        separators=(
            ",",
            ":",
        ),
    ).encode(
        "utf-8"
    )


def test_valid_full_history_bridge():
    result = build_current_ivv_total_return_surface(
        raw_canonical=
            canonical_raw(),

        raw_yahoo=
            make_yahoo_payload(),

        runtime_asof_date=
            "2024-11-06",

        # Deliberately stop one session before the newest
        # raw Yahoo row. Provider must not use the 11-06 row.
        last_completed_us_session=
            "2024-11-05",
    )

    assert result.runtime_eligible is True

    assert (
        result.source_provider
        ==
        SOURCE_PROVIDER_ID
    )

    assert (
        result.core_return_rule_id
        ==
        CORE_RETURN_RULE_ID
    )

    assert (
        result.frozen_session_count
        ==
        FROZEN_EXPECTED_ROW_COUNT
    )

    assert (
        result.post_anchor_session_count
        ==
        2
    )

    assert (
        result.stitched_session_count
        ==
        FROZEN_EXPECTED_ROW_COUNT
        +
        2
    )

    assert (
        result.latest_session
        ==
        "2024-11-05"
    )

    assert (
        result.rows[-1].date
        ==
        "2024-11-05"
    )

    assert (
        result.source_cycle_signature_match
        is True
    )

    assert (
        result.final_schedule_signature_match
        is True
    )

    assert (
        result.max_daily_return_abs_error
        <
        1e-12
    )

    assert len(
        result.stitched_series_sha256
    ) == 64

    assert len(
        result.provider_decision_sha256
    ) == 64

    assert (
        result.broker_post_authorized
        is False
    )

    assert (
        result.live_execution_authorized
        is False
    )


def test_wrong_symbol_fails_closed():
    with pytest.raises(
        CurrentIVVProviderError,
        match="symbol is not IVV",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                canonical_raw(),

            raw_yahoo=
                make_yahoo_payload(
                    symbol="SPY"
                ),

            runtime_asof_date=
                "2024-11-06",

            last_completed_us_session=
                "2024-11-05",
        )


def test_wrong_timezone_fails_closed():
    with pytest.raises(
        CurrentIVVProviderError,
        match="exchange timezone",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                canonical_raw(),

            raw_yahoo=
                make_yahoo_payload(
                    timezone_name="UTC"
                ),

            runtime_asof_date=
                "2024-11-06",

            last_completed_us_session=
                "2024-11-05",
        )


def test_missing_frozen_session_fails_closed():
    with pytest.raises(
        CurrentIVVProviderError,
        match="misses frozen canonical sessions",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                canonical_raw(),

            raw_yahoo=
                make_yahoo_payload(
                    drop_session="2010-01-04"
                ),

            runtime_asof_date=
                "2024-11-06",

            last_completed_us_session=
                "2024-11-05",
        )


def test_transport_revision_over_cap_fails_closed():
    with pytest.raises(
        CurrentIVVProviderError,
        match="transport sanity ceiling",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                canonical_raw(),

            raw_yahoo=
                make_yahoo_payload(
                    tamper_session="2015-06-15",
                    tamper_multiplier=1.001,
                ),

            runtime_asof_date=
                "2024-11-06",

            last_completed_us_session=
                "2024-11-05",
        )


def test_completed_session_must_exist():
    with pytest.raises(
        CurrentIVVProviderError,
        match="missing from Yahoo payload",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                canonical_raw(),

            raw_yahoo=
                make_yahoo_payload(),

            runtime_asof_date=
                "2024-11-07",

            last_completed_us_session=
                "2024-11-07",
        )


def test_completed_session_cannot_be_future():
    with pytest.raises(
        CurrentIVVProviderError,
        match="after runtime as-of date",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                canonical_raw(),

            raw_yahoo=
                make_yahoo_payload(),

            runtime_asof_date=
                "2024-11-05",

            last_completed_us_session=
                "2024-11-06",
        )


def test_canonical_hash_is_hard_guard():
    corrupted = (
        canonical_raw()
        +
        b"\n"
    )

    with pytest.raises(
        CurrentIVVProviderError,
        match="SHA-256 mismatch",
    ):
        build_current_ivv_total_return_surface(
            raw_canonical=
                corrupted,

            raw_yahoo=
                make_yahoo_payload(),

            runtime_asof_date=
                "2024-11-06",

            last_completed_us_session=
                "2024-11-05",
        )


def test_frozen_constants_and_safety():
    assert (
        FROZEN_CANONICAL_SHA256
        ==
        "8ba8a567ffc748f138a9d03d78c77a78619081d5b21aee9b6f688ae0414e03c0"
    )

    assert (
        FROZEN_ANCHOR_SESSION
        ==
        "2024-11-01"
    )

    assert (
        TRANSPORT_SANITY_CAP_BP
        ==
        0.10
    )

    assert (
        ABSOLUTE_YAHOO_ADJ_CLOSE_LEVEL_AUTHORITY
        is False
    )

    assert (
        TOTAL_RETURN_GEOMETRY_AUTHORITY
        is True
    )

    assert (
        NETWORK_PERFORMED_BY_PROVIDER
        is False
    )

    assert (
        BROKER_GET_REQUIRED
        is False
    )

    assert (
        BROKER_POST_AUTHORIZED
        is False
    )

    assert (
        LIVE_EXECUTION_AUTHORIZED
        is False
    )
