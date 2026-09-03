from datetime import date

import pytest

from sp1execution.recovery.causal_runtime_inputs_v01 import (
    VALIDATED_RUNTIME_PROVIDER,
)
from sp1execution.recovery.current_sp2_provider_v01 import (
    BOOTSTRAP_CURRENT_STATE,
    BROKER_POSITIONS_CAN_DEFINE_SP2,
    BROKER_POST_AUTHORIZED,
    HISTORICAL_REPLAY_CAN_DEFINE_CURRENT_SP2,
    LIVE_EXECUTION_AUTHORIZED,
    MONTH_END_SIGNAL,
    CurrentSP2ProviderError,
    build_current_sp2_decision,
    parse_ishares_ivv_holdings,
    validated_execution_ticker_map,
)


def make_holdings(
    *,
    asof="Aug 13, 2026",
    top1=("NVDA", "NVIDIA CORP", "8.12", "73640313668.10"),
    top2=("AAPL", "APPLE INC", "6.67", "60503902922.66"),
    top3=("MSFT", "MICROSOFT", "5.49", "49810352724.96"),
):
    lines = [
        "iShares Core S&P 500 ETF",
        f'Fund Holdings as of,"{asof}"',
        "Inception Date,May 15, 2000",
        "",
        "",
        "",
        "",
        "",
        "",
        (
            "Ticker,Name,Sector,Asset Class,"
            "Market Value,Weight (%),"
            "Notional Value,Quantity,Price,"
            "Location,Exchange,Currency"
        ),
    ]

    for ticker, name, weight, mv in (
        top1,
        top2,
        top3,
    ):
        lines.append(
            f"{ticker},{name},Technology,Equity,"
            f"{mv},{weight},{mv},1,1,"
            "United States,NASDAQ,USD"
        )

    # Ensure a realistic S&P-500-like holdings count.
    for i in range(497):
        ticker = f"D{i:03d}"
        weight = (
            1
            /
            (1000 + i)
        )

        mv = (
            1_000_000
            -
            i
        )

        lines.append(
            f"{ticker},Dummy {i},Other,Equity,"
            f"{mv},{weight:.8f},{mv},1,1,"
            "United States,NYSE,USD"
        )

    text = "\n".join(
        lines
    ) + "\n"

    return text.encode(
        "utf-8"
    )


def test_parse_current_observed_shape():
    x = parse_ishares_ivv_holdings(
        make_holdings()
    )

    assert x.source_asof_date == date(
        2026,
        8,
        13,
    )

    assert (
        x.equity_holding_count
        ==
        500
    )

    assert x.top1.source_ticker == "NVDA"
    assert x.top2.source_ticker == "AAPL"
    assert x.top3.source_ticker == "MSFT"

    assert (
        str(
            x.top2_vs_top3_weight_gap_pp
        )
        ==
        "1.18"
    )

    assert len(
        x.raw_sha256
    ) == 64


def test_bootstrap_current_state():
    result = build_current_sp2_decision(
        raw_holdings=
            make_holdings(),

        runtime_asof_date=
            "2026-08-15",

        mode=
            BOOTSTRAP_CURRENT_STATE,

        effective_date=
            "2026-08-15",
    )

    assert result.source_ranked_tickers == (
        "NVDA",
        "AAPL",
    )

    assert result.execution_ranked_tickers == (
        "NVDA_US_EQ",
        "AAPL_US_EQ",
    )

    assert result.composition.ranked_tickers == (
        "NVDA_US_EQ",
        "AAPL_US_EQ",
    )

    assert (
        result.composition.source_kind
        ==
        VALIDATED_RUNTIME_PROVIDER
    )

    assert (
        result.composition.runtime_eligible
        is True
    )

    assert (
        result.bootstrap_requires_uninitialized_durable_membership
        is True
    )

    assert (
        result.monthly_signal_calendar_validation_required
        is False
    )

    assert len(
        result.decision_sha256
    ) == 64


def test_rank_swap_preserves_same_membership_set():
    result = build_current_sp2_decision(
        raw_holdings=
            make_holdings(
                top1=(
                    "AAPL",
                    "APPLE INC",
                    "8.12",
                    "73640313668.10",
                ),
                top2=(
                    "NVDA",
                    "NVIDIA CORP",
                    "6.67",
                    "60503902922.66",
                ),
            ),

        runtime_asof_date=
            "2026-08-15",

        mode=
            BOOTSTRAP_CURRENT_STATE,

        effective_date=
            "2026-08-15",
    )

    assert set(
        result.execution_ranked_tickers
    ) == {
        "AAPL_US_EQ",
        "NVDA_US_EQ",
    }


def test_unknown_future_top2_mapping_fails_closed():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="has not yet been validated",
    ):
        build_current_sp2_decision(
            raw_holdings=
                make_holdings(
                    top2=(
                        "MSFT",
                        "MICROSOFT",
                        "6.67",
                        "60503902922.66",
                    ),
                    top3=(
                        "AAPL",
                        "APPLE INC",
                        "5.49",
                        "49810352724.96",
                    ),
                ),

            runtime_asof_date=
                "2026-08-15",

            mode=
                BOOTSTRAP_CURRENT_STATE,

            effective_date=
                "2026-08-15",
        )


def test_bootstrap_stale_source_fails():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="stale",
    ):
        build_current_sp2_decision(
            raw_holdings=
                make_holdings(
                    asof="Aug 01, 2026"
                ),

            runtime_asof_date=
                "2026-08-15",

            mode=
                BOOTSTRAP_CURRENT_STATE,

            effective_date=
                "2026-08-15",
        )


def test_future_source_fails():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="future-dated",
    ):
        build_current_sp2_decision(
            raw_holdings=
                make_holdings(
                    asof="Aug 16, 2026"
                ),

            runtime_asof_date=
                "2026-08-15",

            mode=
                BOOTSTRAP_CURRENT_STATE,

            effective_date=
                "2026-08-15",
        )


def test_bootstrap_effective_date_is_runtime_asof():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="must equal runtime as-of",
    ):
        build_current_sp2_decision(
            raw_holdings=
                make_holdings(),

            runtime_asof_date=
                "2026-08-15",

            mode=
                BOOTSTRAP_CURRENT_STATE,

            effective_date=
                "2026-08-14",
        )


def test_month_end_signal_exact_source_date():
    result = build_current_sp2_decision(
        raw_holdings=
            make_holdings(
                asof="Jul 31, 2026"
            ),

        runtime_asof_date=
            "2026-08-03",

        mode=
            MONTH_END_SIGNAL,

        expected_signal_date=
            "2026-07-31",

        effective_date=
            "2026-08-03",
    )

    assert result.composition.signal_date == date(
        2026,
        7,
        31,
    )

    assert result.composition.effective_date == date(
        2026,
        8,
        3,
    )

    assert (
        result.monthly_signal_calendar_validation_required
        is True
    )

    assert (
        result.bootstrap_requires_uninitialized_durable_membership
        is False
    )


def test_month_end_signal_rejects_wrong_source_date():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="required monthly signal date",
    ):
        build_current_sp2_decision(
            raw_holdings=
                make_holdings(
                    asof="Jul 30, 2026"
                ),

            runtime_asof_date=
                "2026-08-03",

            mode=
                MONTH_END_SIGNAL,

            expected_signal_date=
                "2026-07-31",

            effective_date=
                "2026-08-03",
        )


def test_month_end_signal_requires_later_effective_date():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="must follow signal date",
    ):
        build_current_sp2_decision(
            raw_holdings=
                make_holdings(
                    asof="Jul 31, 2026"
                ),

            runtime_asof_date=
                "2026-07-31",

            mode=
                MONTH_END_SIGNAL,

            expected_signal_date=
                "2026-07-31",

            effective_date=
                "2026-07-31",
        )


def test_ambiguous_top2_boundary_fails():
    with pytest.raises(
        CurrentSP2ProviderError,
        match="boundary is ambiguous",
    ):
        parse_ishares_ivv_holdings(
            make_holdings(
                top2=(
                    "AAPL",
                    "APPLE INC",
                    "6.00",
                    "60000000000",
                ),
                top3=(
                    "MSFT",
                    "MICROSOFT",
                    "6.00",
                    "60000000000",
                ),
            )
        )


def test_bad_fund_identity_fails():
    raw = make_holdings().replace(
        b"iShares Core S&P 500 ETF",
        b"Wrong Fund",
        1,
    )

    with pytest.raises(
        CurrentSP2ProviderError,
        match="fund identity",
    ):
        parse_ishares_ivv_holdings(
            raw
        )


def test_execution_mapping_is_narrow_and_explicit():
    x = validated_execution_ticker_map()

    assert x == {
        "AAPL": "AAPL_US_EQ",
        "NVDA": "NVDA_US_EQ",
    }

    assert "MSFT" not in x


def test_global_safety_guards():
    assert LIVE_EXECUTION_AUTHORIZED is False
    assert BROKER_POST_AUTHORIZED is False
    assert BROKER_POSITIONS_CAN_DEFINE_SP2 is False

    assert (
        HISTORICAL_REPLAY_CAN_DEFINE_CURRENT_SP2
        is False
    )
