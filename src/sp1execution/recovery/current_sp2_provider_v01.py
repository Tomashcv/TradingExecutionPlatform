"""
A6C1B — current causal SP2 membership provider.

Pure parser / provider semantics for official iShares IVV holdings.

This module deliberately performs no HTTP request itself.

It converts an immutable raw holdings snapshot into a typed
RuntimeSP2Composition only after:

- validating fund identity;
- validating source as-of date;
- ranking equity holdings by published Weight (%);
- requiring an unambiguous Top-2;
- translating only through a prevalidated execution-symbol mapping;
- enforcing BOOTSTRAP_CURRENT_STATE or MONTH_END_SIGNAL semantics.

The provider does NOT:
- use Trading 212 positions to choose membership;
- use frozen historical files as current state;
- rebalance on Top-1 / Top-2 rank swaps when the set is unchanged;
- infer future Trading 212 symbols;
- authorize broker POSTs or live trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import csv
import json

from sp1execution.recovery.causal_runtime_inputs_v01 import (
    VALIDATED_RUNTIME_PROVIDER,
    RuntimeSP2Composition,
)


SOURCE_PROVIDER_ID = (
    "ISHARES_IVV_OFFICIAL_HOLDINGS_PROXY_V1"
)

EXPECTED_FUND_NAME = (
    "iShares Core S&P 500 ETF"
)

BOOTSTRAP_CURRENT_STATE = (
    "BOOTSTRAP_CURRENT_STATE"
)

MONTH_END_SIGNAL = (
    "MONTH_END_SIGNAL"
)

_ALLOWED_MODES = {
    BOOTSTRAP_CURRENT_STATE,
    MONTH_END_SIGNAL,
}

MAX_BOOTSTRAP_SOURCE_AGE_CALENDAR_DAYS = 7

MIN_EXPECTED_EQUITY_HOLDINGS = 490

EXECUTION_MAPPING_VERSION = (
    "SP2_T212_EXECUTION_MAPPING_V1"
)

# These are execution-symbol mappings already observed/validated
# in Trading 212 Demo during A5C/A6A.
#
# This dictionary is NOT a membership forecast.
#
# If a future Top-2 contains another source ticker, the provider
# fails closed until that execution mapping is separately validated.
_EXECUTION_TICKER_MAP = {
    "AAPL": "AAPL_US_EQ",
    "NVDA": "NVDA_US_EQ",
}

LIVE_EXECUTION_AUTHORIZED = False
BROKER_POST_AUTHORIZED = False
BROKER_POSITIONS_CAN_DEFINE_SP2 = False
HISTORICAL_REPLAY_CAN_DEFINE_CURRENT_SP2 = False


class CurrentSP2ProviderError(ValueError):
    """Fail-closed current SP2 provider error."""


def _as_date(
    value: object,
    field: str,
) -> date:

    if isinstance(value, datetime):
        raise CurrentSP2ProviderError(
            f"{field}: datetime not accepted"
        )

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(
            str(value)
        )
    except Exception as exc:
        raise CurrentSP2ProviderError(
            f"{field}: invalid ISO date"
        ) from exc


def _decimal(
    value: object,
    field: str,
) -> Decimal:

    try:
        x = Decimal(
            str(value)
        )
    except (
        InvalidOperation,
        ValueError,
    ) as exc:
        raise CurrentSP2ProviderError(
            f"{field}: invalid decimal"
        ) from exc

    if not x.is_finite():
        raise CurrentSP2ProviderError(
            f"{field}: non-finite decimal"
        )

    return x


@dataclass(frozen=True)
class IVVEquityHolding:
    source_ticker: str
    name: str
    weight_pct: Decimal
    market_value: Decimal
    currency: str


@dataclass(frozen=True)
class IVVHoldingsSnapshot:
    source_asof_date: date

    raw_sha256: str

    equity_holding_count: int

    ranked_equities: tuple[
        IVVEquityHolding,
        ...
    ]

    source_provider: str = (
        SOURCE_PROVIDER_ID
    )

    @property
    def top1(self) -> IVVEquityHolding:
        return self.ranked_equities[0]

    @property
    def top2(self) -> IVVEquityHolding:
        return self.ranked_equities[1]

    @property
    def top3(self) -> IVVEquityHolding:
        return self.ranked_equities[2]

    @property
    def top2_set(self) -> frozenset[str]:
        return frozenset({
            self.top1.source_ticker,
            self.top2.source_ticker,
        })

    @property
    def top2_vs_top3_weight_gap_pp(
        self,
    ) -> Decimal:
        return (
            self.top2.weight_pct
            -
            self.top3.weight_pct
        )


@dataclass(frozen=True)
class CurrentSP2ProviderDecision:
    schema: str

    mode: str

    runtime_asof_date: date

    source_snapshot: IVVHoldingsSnapshot

    source_ranked_tickers: tuple[
        str,
        str,
    ]

    execution_ranked_tickers: tuple[
        str,
        str,
    ]

    execution_mapping_version: str

    decision_sha256: str

    composition: RuntimeSP2Composition

    bootstrap_requires_uninitialized_durable_membership: bool

    monthly_signal_calendar_validation_required: bool

    broker_post_authorized: bool = False
    live_execution_authorized: bool = False


def parse_ishares_ivv_holdings(
    raw: bytes,
) -> IVVHoldingsSnapshot:

    if not isinstance(
        raw,
        (bytes, bytearray),
    ):
        raise CurrentSP2ProviderError(
            "raw holdings response must be bytes"
        )

    raw = bytes(raw)

    if len(raw) < 1000:
        raise CurrentSP2ProviderError(
            "holdings response suspiciously small"
        )

    raw_hash = sha256(
        raw
    ).hexdigest()

    try:
        text = raw.decode(
            "utf-8-sig",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise CurrentSP2ProviderError(
            "holdings response is not valid UTF-8"
        ) from exc

    lines = text.splitlines()

    if not lines:
        raise CurrentSP2ProviderError(
            "empty holdings response"
        )

    if lines[0].strip() != EXPECTED_FUND_NAME:
        raise CurrentSP2ProviderError(
            "unexpected fund identity"
        )

    source_asof = None

    for line in lines[:20]:

        if not line.startswith(
            "Fund Holdings as of,"
        ):
            continue

        row = next(
            csv.reader([line])
        )

        if len(row) < 2:
            continue

        try:
            source_asof = datetime.strptime(
                row[1].strip(),
                "%b %d, %Y",
            ).date()
        except ValueError as exc:
            raise CurrentSP2ProviderError(
                "invalid Fund Holdings as-of date"
            ) from exc

        break

    if source_asof is None:
        raise CurrentSP2ProviderError(
            "Fund Holdings as-of date missing"
        )

    header_index = None

    for i, line in enumerate(lines):

        row = next(
            csv.reader([line])
        )

        if (
            row
            and
            row[0].strip() == "Ticker"
            and
            "Weight (%)" in row
            and
            "Asset Class" in row
            and
            "Market Value" in row
        ):
            header_index = i
            break

    if header_index is None:
        raise CurrentSP2ProviderError(
            "holdings CSV header missing"
        )

    reader = csv.DictReader(
        StringIO(
            "\n".join(
                lines[header_index:]
            )
        )
    )

    equities: list[
        IVVEquityHolding
    ] = []

    seen_tickers: set[str] = set()

    for row in reader:

        if not isinstance(
            row,
            dict,
        ):
            continue

        asset_class = str(
            row.get(
                "Asset Class",
                "",
            )
        ).strip()

        if asset_class.lower() != "equity":
            continue

        ticker = str(
            row.get(
                "Ticker",
                "",
            )
        ).strip()

        if not ticker:
            continue

        if ticker in seen_tickers:
            raise CurrentSP2ProviderError(
                f"duplicate equity ticker: {ticker}"
            )

        weight_raw = str(
            row.get(
                "Weight (%)",
                "",
            )
        ).replace(
            ",",
            "",
        ).strip()

        market_value_raw = str(
            row.get(
                "Market Value",
                "",
            )
        ).replace(
            ",",
            "",
        ).strip()

        try:
            weight = _decimal(
                weight_raw,
                f"{ticker}.weight",
            )

            market_value = _decimal(
                market_value_raw,
                f"{ticker}.market_value",
            )

        except CurrentSP2ProviderError:
            continue

        if weight < 0:
            continue

        if market_value < 0:
            continue

        seen_tickers.add(
            ticker
        )

        equities.append(
            IVVEquityHolding(
                source_ticker=ticker,

                name=str(
                    row.get(
                        "Name",
                        "",
                    )
                ).strip(),

                weight_pct=weight,

                market_value=market_value,

                currency=str(
                    row.get(
                        "Currency",
                        "",
                    )
                ).strip().upper(),
            )
        )

    if (
        len(equities)
        <
        MIN_EXPECTED_EQUITY_HOLDINGS
    ):
        raise CurrentSP2ProviderError(
            "implausibly few IVV equity holdings"
        )

    equities.sort(
        key=lambda row: (
            -row.weight_pct,
            -row.market_value,
            row.source_ticker,
        )
    )

    if len(equities) < 3:
        raise CurrentSP2ProviderError(
            "at least three ranked equities required"
        )

    # Published weight is primary ranking authority.
    #
    # Market value resolves only displayed-weight rounding ties.
    # If both fields tie at the #2/#3 boundary, ranking is not
    # sufficiently identified and the provider fails closed.
    second = equities[1]
    third = equities[2]

    if (
        second.weight_pct
        ==
        third.weight_pct
        and
        second.market_value
        ==
        third.market_value
    ):
        raise CurrentSP2ProviderError(
            "Top-2 boundary is ambiguous"
        )

    return IVVHoldingsSnapshot(
        source_asof_date=
            source_asof,

        raw_sha256=
            raw_hash,

        equity_holding_count=
            len(equities),

        ranked_equities=
            tuple(equities),
    )


def validated_execution_ticker_map(
) -> dict[str, str]:
    return dict(
        _EXECUTION_TICKER_MAP
    )


def _map_execution_ticker(
    source_ticker: str,
) -> str:

    mapped = _EXECUTION_TICKER_MAP.get(
        source_ticker
    )

    if mapped is None:
        raise CurrentSP2ProviderError(
            "current Top-2 contains an execution ticker "
            "whose Trading 212 mapping has not yet been "
            f"validated: {source_ticker}"
        )

    return mapped


def build_current_sp2_decision(
    *,
    raw_holdings: bytes,
    runtime_asof_date: object,
    mode: str,
    effective_date: object,
    expected_signal_date: object | None = None,
) -> CurrentSP2ProviderDecision:

    runtime_asof = _as_date(
        runtime_asof_date,
        "runtime_asof_date",
    )

    effective = _as_date(
        effective_date,
        "effective_date",
    )

    mode = str(
        mode
    ).strip().upper()

    if mode not in _ALLOWED_MODES:
        raise CurrentSP2ProviderError(
            "invalid provider mode"
        )

    snapshot = parse_ishares_ivv_holdings(
        raw_holdings
    )

    source_date = (
        snapshot.source_asof_date
    )

    if source_date > runtime_asof:
        raise CurrentSP2ProviderError(
            "holdings source is future-dated"
        )

    if effective > runtime_asof:
        raise CurrentSP2ProviderError(
            "effective date is future-dated"
        )

    bootstrap_guard = False
    monthly_calendar_guard = False

    if mode == BOOTSTRAP_CURRENT_STATE:

        age = (
            runtime_asof
            -
            source_date
        ).days

        if (
            age
            >
            MAX_BOOTSTRAP_SOURCE_AGE_CALENDAR_DAYS
        ):
            raise CurrentSP2ProviderError(
                "bootstrap holdings source is stale"
            )

        if effective != runtime_asof:
            raise CurrentSP2ProviderError(
                "BOOTSTRAP_CURRENT_STATE effective date "
                "must equal runtime as-of date"
            )

        if expected_signal_date is not None:
            raise CurrentSP2ProviderError(
                "bootstrap mode must not receive "
                "expected_signal_date"
            )

        bootstrap_guard = True

    else:

        if expected_signal_date is None:
            raise CurrentSP2ProviderError(
                "MONTH_END_SIGNAL requires "
                "expected_signal_date"
            )

        expected_signal = _as_date(
            expected_signal_date,
            "expected_signal_date",
        )

        if source_date != expected_signal:
            raise CurrentSP2ProviderError(
                "holdings source date does not equal "
                "the required monthly signal date"
            )

        if effective <= source_date:
            raise CurrentSP2ProviderError(
                "MONTH_END_SIGNAL effective date must "
                "follow signal date"
            )

        # Exact US-session T+1 validation is intentionally delegated
        # to the runtime calendar layer. A6C1B cannot guess holidays.
        monthly_calendar_guard = True

    source_ranked = (
        snapshot.top1.source_ticker,
        snapshot.top2.source_ticker,
    )

    execution_ranked = (
        _map_execution_ticker(
            source_ranked[0]
        ),
        _map_execution_ticker(
            source_ranked[1]
        ),
    )

    canonical_decision = {
        "schema":
            "sp2_recovery_a6c1b_current_sp2_provider_decision_v1",

        "provider":
            SOURCE_PROVIDER_ID,

        "mode":
            mode,

        "runtime_asof_date":
            runtime_asof.isoformat(),

        "source_asof_date":
            source_date.isoformat(),

        "effective_date":
            effective.isoformat(),

        "raw_holdings_sha256":
            snapshot.raw_sha256,

        "source_ranked_tickers":
            list(source_ranked),

        "execution_ranked_tickers":
            list(execution_ranked),

        "execution_mapping_version":
            EXECUTION_MAPPING_VERSION,
    }

    decision_bytes = json.dumps(
        canonical_decision,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
    ).encode(
        "utf-8"
    )

    decision_hash = sha256(
        decision_bytes
    ).hexdigest()

    composition = RuntimeSP2Composition(
        asof_date=
            runtime_asof,

        signal_date=
            source_date,

        effective_date=
            effective,

        ranked_tickers=
            execution_ranked,

        source_kind=
            VALIDATED_RUNTIME_PROVIDER,

        source_id=(
            f"{SOURCE_PROVIDER_ID}:"
            f"{mode}:"
            f"{source_date.isoformat()}:"
            f"{EXECUTION_MAPPING_VERSION}"
        ),

        source_sha256=
            decision_hash,
    )

    return CurrentSP2ProviderDecision(
        schema=
            "sp2_recovery_a6c1b_current_sp2_provider_decision_v1",

        mode=
            mode,

        runtime_asof_date=
            runtime_asof,

        source_snapshot=
            snapshot,

        source_ranked_tickers=
            source_ranked,

        execution_ranked_tickers=
            execution_ranked,

        execution_mapping_version=
            EXECUTION_MAPPING_VERSION,

        decision_sha256=
            decision_hash,

        composition=
            composition,

        bootstrap_requires_uninitialized_durable_membership=
            bootstrap_guard,

        monthly_signal_calendar_validation_required=
            monthly_calendar_guard,
    )
