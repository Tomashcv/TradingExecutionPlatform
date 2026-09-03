"""
A6C2B — current causal IVV total-return market-data provider.

Pure provider.

The frozen CORE_RETURN trigger geometry is:

    IVV adjusted-close / total-return geometry

The frozen historical surface is stored as normalized ``ivv_nav``.

The current transport is Yahoo Chart IVV adjusted close, but absolute
Yahoo adjusted-close levels are NOT treated as authoritative.

Instead:

1. full frozen-history session coverage must match;
2. current Yahoo return revisions must remain under a narrow transport
   sanity ceiling;
3. the current Yahoo history must reproduce the exact frozen source
   cycle signature;
4. it must reproduce the exact frozen D40/H378 final schedule;
5. post-frozen history is attached by adjusted-close return ratios to
   the frozen 2024-11-01 IVV NAV anchor.

No HTTP request is performed by this module.

The caller must separately supply the last completed US session.
This provider deliberately does not guess whether today's bar is
complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from io import StringIO
from zoneinfo import ZoneInfo

import csv
import json
import math

from sp1execution.recovery.causal_compiler_v01 import (
    RecoveryInputRow,
    compile_final_schedule,
    compile_source_cycles,
)


SOURCE_PROVIDER_ID = (
    "YAHOO_CHART_IVV_ADJCLOSE_FULLHISTORY_BRIDGE_V1"
)

EXPECTED_SYMBOL = "IVV"
EXPECTED_CURRENCY = "USD"
EXPECTED_EXCHANGE_TIMEZONE = "America/New_York"
EXPECTED_DATA_GRANULARITY = "1d"
EXPECTED_INSTRUMENT_TYPE = "ETF"

FROZEN_CANONICAL_SHA256 = (
    "8ba8a567ffc748f138a9d03d78c77a78619081d5b21aee9b6f688ae0414e03c0"
)

FROZEN_FIRST_SESSION = "2001-01-03"
FROZEN_ANCHOR_SESSION = "2024-11-01"
FROZEN_EXPECTED_ROW_COUNT = 5996

# 0.10 basis point in decimal daily-return units.
#
# This is an input-transport sanity ceiling, not a strategy threshold.
# Frozen research thresholds remain untouched.
TRANSPORT_SANITY_CAP_DECIMAL_RETURN = 1e-5
TRANSPORT_SANITY_CAP_BP = 0.10

CORE_RETURN_RULE_ID = (
    "SP2_RECOVERY_CORE_RETURN_D40_H378_V1"
)

LIVE_EXECUTION_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False
BROKER_GET_REQUIRED = False
NETWORK_PERFORMED_BY_PROVIDER = False

ABSOLUTE_YAHOO_ADJ_CLOSE_LEVEL_AUTHORITY = False
TOTAL_RETURN_GEOMETRY_AUTHORITY = True


class CurrentIVVProviderError(
    ValueError
):
    """Fail-closed current IVV provider error."""


@dataclass(frozen=True)
class CurrentIVVTotalReturnSurface:
    schema: str

    source_provider: str
    core_return_rule_id: str

    runtime_asof_date: date
    last_completed_us_session: str

    canonical_raw_sha256: str
    yahoo_raw_sha256: str

    frozen_session_count: int
    post_anchor_session_count: int
    stitched_session_count: int

    frozen_first_session: str
    frozen_anchor_session: str

    latest_session: str
    latest_value: float

    running_ath_session: str
    running_ath_value: float
    latest_positive_drawdown: float

    max_normalized_level_rel_error: float
    max_daily_return_abs_error: float
    max_positive_drawdown_abs_error: float

    source_cycle_signature_match: bool
    final_schedule_signature_match: bool

    stitched_series_sha256: str
    provider_decision_sha256: str

    rows: tuple[
        RecoveryInputRow,
        ...
    ]

    runtime_eligible: bool = True

    broker_post_authorized: bool = False
    live_execution_authorized: bool = False


def _as_date(
    value: object,
    field: str,
) -> date:

    if isinstance(
        value,
        datetime,
    ):
        raise CurrentIVVProviderError(
            f"{field}: datetime not accepted"
        )

    if isinstance(
        value,
        date,
    ):
        return value

    try:
        return date.fromisoformat(
            str(value)
        )

    except Exception as exc:
        raise CurrentIVVProviderError(
            f"{field}: invalid ISO date"
        ) from exc


def _finite_positive(
    value: object,
    field: str,
) -> float:

    try:
        x = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:
        raise CurrentIVVProviderError(
            f"{field}: invalid numeric value"
        ) from exc

    if (
        not math.isfinite(x)
        or
        x <= 0
    ):
        raise CurrentIVVProviderError(
            f"{field}: expected finite positive value"
        )

    return x


def _parse_canonical(
    raw: bytes,
) -> tuple[
    tuple[
        RecoveryInputRow,
        ...
    ],
    dict[str, float],
]:

    if not isinstance(
        raw,
        (bytes, bytearray),
    ):
        raise CurrentIVVProviderError(
            "canonical input must be bytes"
        )

    raw = bytes(
        raw
    )

    actual_hash = sha256(
        raw
    ).hexdigest()

    if (
        actual_hash
        !=
        FROZEN_CANONICAL_SHA256
    ):
        raise CurrentIVVProviderError(
            "frozen canonical SHA-256 mismatch"
        )

    try:
        text = raw.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise CurrentIVVProviderError(
            "canonical input is not valid UTF-8"
        ) from exc

    reader = csv.DictReader(
        StringIO(
            text
        )
    )

    fields = list(
        reader.fieldnames
        or []
    )

    if fields != [
        "date",
        "sp2_nav",
        "ivv_nav",
    ]:
        raise CurrentIVVProviderError(
            "unexpected frozen canonical schema"
        )

    rows = []
    values = {}
    previous = None

    for source_row in reader:

        session = str(
            source_row[
                "date"
            ]
        ).strip()

        try:
            parsed_date = date.fromisoformat(
                session
            )

        except ValueError as exc:
            raise CurrentIVVProviderError(
                f"invalid canonical session: {session}"
            ) from exc

        value = _finite_positive(
            source_row[
                "ivv_nav"
            ],
            f"canonical {session} ivv_nav",
        )

        if previous is not None:
            if parsed_date <= previous:
                raise CurrentIVVProviderError(
                    "canonical sessions not strictly increasing"
                )

        previous = parsed_date

        if session in values:
            raise CurrentIVVProviderError(
                f"duplicate canonical session: {session}"
            )

        values[
            session
        ] = value

        rows.append(
            RecoveryInputRow(
                date=session,
                close=value,
            )
        )

    if (
        len(rows)
        !=
        FROZEN_EXPECTED_ROW_COUNT
    ):
        raise CurrentIVVProviderError(
            "unexpected frozen canonical row count"
        )

    if rows[0].date != FROZEN_FIRST_SESSION:
        raise CurrentIVVProviderError(
            "unexpected frozen canonical first session"
        )

    if rows[-1].date != FROZEN_ANCHOR_SESSION:
        raise CurrentIVVProviderError(
            "unexpected frozen canonical anchor session"
        )

    return (
        tuple(
            rows
        ),
        values,
    )


def _parse_yahoo_adjusted_close(
    raw: bytes,
) -> tuple[
    dict[str, float],
    dict[str, object],
]:

    if not isinstance(
        raw,
        (bytes, bytearray),
    ):
        raise CurrentIVVProviderError(
            "Yahoo input must be bytes"
        )

    raw = bytes(
        raw
    )

    if len(raw) < 1000:
        raise CurrentIVVProviderError(
            "Yahoo response suspiciously small"
        )

    try:
        payload = json.loads(
            raw
        )

    except Exception as exc:
        raise CurrentIVVProviderError(
            "invalid Yahoo JSON"
        ) from exc

    chart = payload.get(
        "chart"
    )

    if not isinstance(
        chart,
        dict,
    ):
        raise CurrentIVVProviderError(
            "Yahoo chart object missing"
        )

    if chart.get(
        "error"
    ) is not None:
        raise CurrentIVVProviderError(
            "Yahoo chart returned an error"
        )

    results = chart.get(
        "result"
    )

    if (
        not isinstance(
            results,
            list,
        )
        or
        len(results) != 1
    ):
        raise CurrentIVVProviderError(
            "expected one Yahoo chart result"
        )

    result = results[0]

    if not isinstance(
        result,
        dict,
    ):
        raise CurrentIVVProviderError(
            "Yahoo result is not an object"
        )

    meta = result.get(
        "meta",
        {},
    )

    if not isinstance(
        meta,
        dict,
    ):
        raise CurrentIVVProviderError(
            "Yahoo metadata missing"
        )

    if str(
        meta.get(
            "symbol",
            "",
        )
    ).upper() != EXPECTED_SYMBOL:
        raise CurrentIVVProviderError(
            "Yahoo symbol is not IVV"
        )

    if str(
        meta.get(
            "currency",
            "",
        )
    ).upper() != EXPECTED_CURRENCY:
        raise CurrentIVVProviderError(
            "Yahoo currency is not USD"
        )

    if str(
        meta.get(
            "exchangeTimezoneName",
            "",
        )
    ) != EXPECTED_EXCHANGE_TIMEZONE:
        raise CurrentIVVProviderError(
            "unexpected Yahoo exchange timezone"
        )

    if str(
        meta.get(
            "dataGranularity",
            "",
        )
    ) != EXPECTED_DATA_GRANULARITY:
        raise CurrentIVVProviderError(
            "Yahoo data granularity is not 1d"
        )

    instrument_type = str(
        meta.get(
            "instrumentType",
            "",
        )
    )

    if (
        instrument_type
        and
        instrument_type
        !=
        EXPECTED_INSTRUMENT_TYPE
    ):
        raise CurrentIVVProviderError(
            "unexpected Yahoo instrument type"
        )

    timestamps = result.get(
        "timestamp"
    )

    indicators = result.get(
        "indicators",
        {},
    )

    if not isinstance(
        timestamps,
        list,
    ):
        raise CurrentIVVProviderError(
            "Yahoo timestamp array missing"
        )

    if not isinstance(
        indicators,
        dict,
    ):
        raise CurrentIVVProviderError(
            "Yahoo indicators object missing"
        )

    adjblocks = indicators.get(
        "adjclose"
    )

    if (
        not isinstance(
            adjblocks,
            list,
        )
        or
        len(adjblocks) != 1
    ):
        raise CurrentIVVProviderError(
            "Yahoo adjusted-close block missing"
        )

    adjusted = adjblocks[0].get(
        "adjclose"
    )

    if not isinstance(
        adjusted,
        list,
    ):
        raise CurrentIVVProviderError(
            "Yahoo adjusted-close array missing"
        )

    if len(
        timestamps
    ) != len(
        adjusted
    ):
        raise CurrentIVVProviderError(
            "Yahoo timestamp/adjusted-close length mismatch"
        )

    ny = ZoneInfo(
        EXPECTED_EXCHANGE_TIMEZONE
    )

    values = {}

    for timestamp, raw_value in zip(
        timestamps,
        adjusted,
    ):

        if (
            timestamp is None
            or
            raw_value is None
        ):
            continue

        try:
            instant = datetime.fromtimestamp(
                int(
                    timestamp
                ),
                tz=timezone.utc,
            )

        except (
            TypeError,
            ValueError,
            OSError,
        ):
            continue

        session = (
            instant
            .astimezone(
                ny
            )
            .date()
            .isoformat()
        )

        value = _finite_positive(
            raw_value,
            f"Yahoo {session} adjusted close",
        )

        if session in values:
            raise CurrentIVVProviderError(
                f"duplicate Yahoo session: {session}"
            )

        values[
            session
        ] = value

    if not values:
        raise CurrentIVVProviderError(
            "Yahoo adjusted-close history is empty"
        )

    return (
        values,
        meta,
    )


def _positive_drawdown_path(
    values: dict[str, float],
    sessions: list[str],
) -> dict[str, float]:

    peak = None
    result = {}

    for session in sessions:

        value = values[
            session
        ]

        if (
            peak is None
            or
            value > peak
        ):
            peak = value

        result[
            session
        ] = (
            1.0
            -
            value
            /
            peak
        )

    return result


def _round_target(
    value: object,
) -> object:

    if value is None:
        return None

    try:
        return round(
            float(
                value
            ),
            12,
        )

    except (
        TypeError,
        ValueError,
    ):
        return value


def _event_signature(
    event: object,
) -> tuple[object, ...]:

    return (
        getattr(
            event,
            "event_type",
            None,
        ),

        getattr(
            event,
            "signal_date",
            None,
        ),

        getattr(
            event,
            "execution_date",
            None,
        ),

        _round_target(
            getattr(
                event,
                "target_sleeve",
                None,
            )
        ),
    )


def _cycle_signature(
    cycle: object,
) -> tuple[object, ...]:

    return (
        getattr(
            cycle,
            "cycle_id",
            None,
        ),

        getattr(
            cycle,
            "old_ath_date",
            None,
        ),

        tuple(
            _event_signature(
                event
            )
            for event
            in getattr(
                cycle,
                "events",
                (),
            )
        ),
    )


_SCHEDULE_FIELDS = (
    "cycle_id",
    "event_type",
    "final_execution_date",
    "target_sleeve",
    "source_signal_date",
    "source_execution_t_plus_1",
    "old_ath_recovery_date",
    "effective_no_new_cycle_until",
)


def _schedule_signature(
    row: object,
) -> tuple[object, ...]:

    result = []

    for field in _SCHEDULE_FIELDS:

        value = getattr(
            row,
            field,
            None,
        )

        if field == "target_sleeve":
            value = _round_target(
                value
            )

        result.append(
            value
        )

    return tuple(
        result
    )


def _series_hash(
    rows: tuple[
        RecoveryInputRow,
        ...
    ],
) -> str:

    payload = "".join(
        f"{row.date},{float(row.close):.17g}\n"
        for row in rows
    ).encode(
        "utf-8"
    )

    return sha256(
        payload
    ).hexdigest()


def build_current_ivv_total_return_surface(
    *,
    raw_canonical: bytes,
    raw_yahoo: bytes,
    runtime_asof_date: object,
    last_completed_us_session: object,
) -> CurrentIVVTotalReturnSurface:

    runtime_asof = _as_date(
        runtime_asof_date,
        "runtime_asof_date",
    )

    completed_session_date = _as_date(
        last_completed_us_session,
        "last_completed_us_session",
    )

    completed_session = (
        completed_session_date
        .isoformat()
    )

    if completed_session_date > runtime_asof:
        raise CurrentIVVProviderError(
            "last completed US session is after runtime as-of date"
        )

    anchor_date = date.fromisoformat(
        FROZEN_ANCHOR_SESSION
    )

    if completed_session_date <= anchor_date:
        raise CurrentIVVProviderError(
            "last completed US session must be after frozen anchor"
        )

    (
        frozen_rows,
        frozen,
    ) = _parse_canonical(
        raw_canonical
    )

    (
        live,
        _meta,
    ) = _parse_yahoo_adjusted_close(
        raw_yahoo
    )

    live_sessions = sorted(
        live
    )

    latest_live_date = date.fromisoformat(
        live_sessions[-1]
    )

    if latest_live_date > runtime_asof:
        raise CurrentIVVProviderError(
            "Yahoo payload contains a future session"
        )

    if completed_session not in live:
        raise CurrentIVVProviderError(
            "last completed US session missing from Yahoo payload"
        )

    frozen_sessions = [
        row.date
        for row in frozen_rows
    ]

    frozen_set = set(
        frozen_sessions
    )

    missing_frozen = [
        session
        for session in frozen_sessions
        if session not in live
    ]

    if missing_frozen:
        raise CurrentIVVProviderError(
            "Yahoo payload misses frozen canonical sessions"
        )

    extra_frozen_window = [
        session
        for session in live_sessions
        if (
            FROZEN_FIRST_SESSION
            <=
            session
            <=
            FROZEN_ANCHOR_SESSION
            and
            session not in frozen_set
        )
    ]

    if extra_frozen_window:
        raise CurrentIVVProviderError(
            "Yahoo payload contains extra sessions inside frozen window"
        )

    frozen_anchor = frozen[
        FROZEN_FIRST_SESSION
    ]

    live_anchor = live[
        FROZEN_FIRST_SESSION
    ]

    normalized_level_errors = []

    for session in frozen_sessions:

        frozen_normalized = (
            frozen[
                session
            ]
            /
            frozen_anchor
        )

        live_normalized = (
            live[
                session
            ]
            /
            live_anchor
        )

        normalized_level_errors.append(
            abs(
                live_normalized
                /
                frozen_normalized
                -
                1.0
            )
        )

    return_errors = []

    for previous, current in zip(
        frozen_sessions,
        frozen_sessions[
            1:
        ],
    ):

        frozen_return = (
            frozen[
                current
            ]
            /
            frozen[
                previous
            ]
            -
            1.0
        )

        live_return = (
            live[
                current
            ]
            /
            live[
                previous
            ]
            -
            1.0
        )

        return_errors.append(
            abs(
                live_return
                -
                frozen_return
            )
        )

    max_normalized_level_rel_error = max(
        normalized_level_errors
    )

    max_daily_return_abs_error = max(
        return_errors
    )

    if (
        max_daily_return_abs_error
        >
        TRANSPORT_SANITY_CAP_DECIMAL_RETURN
    ):
        raise CurrentIVVProviderError(
            "Yahoo historical return revision exceeds "
            "transport sanity ceiling"
        )

    frozen_dd = _positive_drawdown_path(
        frozen,
        frozen_sessions,
    )

    live_dd = _positive_drawdown_path(
        live,
        frozen_sessions,
    )

    max_positive_drawdown_abs_error = max(
        abs(
            frozen_dd[
                session
            ]
            -
            live_dd[
                session
            ]
        )
        for session in frozen_sessions
    )

    live_frozen_rows = tuple(
        RecoveryInputRow(
            date=session,
            close=live[
                session
            ],
        )
        for session in frozen_sessions
    )

    frozen_cycles = compile_source_cycles(
        frozen_rows
    )

    live_cycles = compile_source_cycles(
        live_frozen_rows
    )

    frozen_cycle_signature = tuple(
        _cycle_signature(
            cycle
        )
        for cycle in frozen_cycles
    )

    live_cycle_signature = tuple(
        _cycle_signature(
            cycle
        )
        for cycle in live_cycles
    )

    source_cycle_signature_match = (
        frozen_cycle_signature
        ==
        live_cycle_signature
    )

    if not source_cycle_signature_match:
        raise CurrentIVVProviderError(
            "Yahoo history changes frozen source-cycle semantics"
        )

    frozen_schedule = compile_final_schedule(
        frozen_rows
    )

    live_schedule = compile_final_schedule(
        live_frozen_rows
    )

    frozen_schedule_signature = tuple(
        _schedule_signature(
            row
        )
        for row in frozen_schedule
    )

    live_schedule_signature = tuple(
        _schedule_signature(
            row
        )
        for row in live_schedule
    )

    final_schedule_signature_match = (
        frozen_schedule_signature
        ==
        live_schedule_signature
    )

    if not final_schedule_signature_match:
        raise CurrentIVVProviderError(
            "Yahoo history changes frozen D40/H378 schedule"
        )

    bridge_frozen_value = frozen[
        FROZEN_ANCHOR_SESSION
    ]

    bridge_live_value = live[
        FROZEN_ANCHOR_SESSION
    ]

    stitched = dict(
        frozen
    )

    post_anchor_sessions = [
        session
        for session in live_sessions
        if (
            FROZEN_ANCHOR_SESSION
            <
            session
            <=
            completed_session
        )
    ]

    if not post_anchor_sessions:
        raise CurrentIVVProviderError(
            "no post-anchor completed Yahoo sessions"
        )

    for session in post_anchor_sessions:

        stitched[
            session
        ] = (
            bridge_frozen_value
            *
            live[
                session
            ]
            /
            bridge_live_value
        )

    stitched_sessions = sorted(
        stitched
    )

    if stitched_sessions[-1] != completed_session:
        raise CurrentIVVProviderError(
            "stitched surface does not end on last completed session"
        )

    stitched_rows = tuple(
        RecoveryInputRow(
            date=session,
            close=stitched[
                session
            ],
        )
        for session in stitched_sessions
    )

    running_ath_value = -math.inf
    running_ath_session = None

    for session in stitched_sessions:

        value = stitched[
            session
        ]

        if value > running_ath_value:

            running_ath_value = value
            running_ath_session = session

    if running_ath_session is None:
        raise CurrentIVVProviderError(
            "cannot identify running ATH"
        )

    latest_session = stitched_sessions[
        -1
    ]

    latest_value = stitched[
        latest_session
    ]

    latest_positive_drawdown = (
        1.0
        -
        latest_value
        /
        running_ath_value
    )

    if (
        latest_positive_drawdown
        <
        -1e-12
    ):
        raise CurrentIVVProviderError(
            "negative positive-drawdown invariant"
        )

    if latest_positive_drawdown < 0:
        latest_positive_drawdown = 0.0

    stitched_hash = _series_hash(
        stitched_rows
    )

    yahoo_hash = sha256(
        bytes(
            raw_yahoo
        )
    ).hexdigest()

    canonical_hash = sha256(
        bytes(
            raw_canonical
        )
    ).hexdigest()

    decision_payload = {
        "schema":
            "sp2_recovery_a6c2b_current_ivv_total_return_surface_v1",

        "source_provider":
            SOURCE_PROVIDER_ID,

        "core_return_rule_id":
            CORE_RETURN_RULE_ID,

        "runtime_asof_date":
            runtime_asof.isoformat(),

        "last_completed_us_session":
            completed_session,

        "canonical_raw_sha256":
            canonical_hash,

        "yahoo_raw_sha256":
            yahoo_hash,

        "stitched_series_sha256":
            stitched_hash,

        "frozen_session_count":
            len(
                frozen_sessions
            ),

        "post_anchor_session_count":
            len(
                post_anchor_sessions
            ),

        "latest_session":
            latest_session,

        "running_ath_session":
            running_ath_session,

        "max_daily_return_abs_error":
            format(
                max_daily_return_abs_error,
                ".17g",
            ),

        "source_cycle_signature_match":
            source_cycle_signature_match,

        "final_schedule_signature_match":
            final_schedule_signature_match,
    }

    provider_decision_sha256 = sha256(
        json.dumps(
            decision_payload,
            sort_keys=True,
            separators=(
                ",",
                ":",
            ),
        ).encode(
            "utf-8"
        )
    ).hexdigest()

    return CurrentIVVTotalReturnSurface(
        schema=
            "sp2_recovery_a6c2b_current_ivv_total_return_surface_v1",

        source_provider=
            SOURCE_PROVIDER_ID,

        core_return_rule_id=
            CORE_RETURN_RULE_ID,

        runtime_asof_date=
            runtime_asof,

        last_completed_us_session=
            completed_session,

        canonical_raw_sha256=
            canonical_hash,

        yahoo_raw_sha256=
            yahoo_hash,

        frozen_session_count=
            len(
                frozen_sessions
            ),

        post_anchor_session_count=
            len(
                post_anchor_sessions
            ),

        stitched_session_count=
            len(
                stitched_rows
            ),

        frozen_first_session=
            FROZEN_FIRST_SESSION,

        frozen_anchor_session=
            FROZEN_ANCHOR_SESSION,

        latest_session=
            latest_session,

        latest_value=
            latest_value,

        running_ath_session=
            running_ath_session,

        running_ath_value=
            running_ath_value,

        latest_positive_drawdown=
            latest_positive_drawdown,

        max_normalized_level_rel_error=
            max_normalized_level_rel_error,

        max_daily_return_abs_error=
            max_daily_return_abs_error,

        max_positive_drawdown_abs_error=
            max_positive_drawdown_abs_error,

        source_cycle_signature_match=
            source_cycle_signature_match,

        final_schedule_signature_match=
            final_schedule_signature_match,

        stitched_series_sha256=
            stitched_hash,

        provider_decision_sha256=
            provider_decision_sha256,

        rows=
            stitched_rows,
    )
