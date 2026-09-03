from datetime import date
from decimal import Decimal

import pytest

from sp1execution.recovery.causal_runtime_inputs_v01 import (
    BROKER_POSITIONS_CAN_DEFINE_SP2,
    BROKER_POST_AUTHORIZED,
    CORE_RETURN_RULE_ID,
    FROZEN_RESEARCH_REPLAY,
    LIVE_EXECUTION_AUTHORIZED,
    VALIDATED_RUNTIME_PROVIDER,
    CausalRuntimeInputError,
    CausalRuntimeInputs,
    RuntimeRecoveryTarget,
    RuntimeSP2Composition,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def sp2(
    *,
    source_kind=VALIDATED_RUNTIME_PROVIDER,
):
    return RuntimeSP2Composition(
        asof_date="2026-08-15",
        signal_date="2026-08-01",
        effective_date="2026-08-03",
        ranked_tickers=(
            "NVDA_US_EQ",
            "AAPL_US_EQ",
        ),
        source_kind=source_kind,
        source_id="runtime-sp2-fixture",
        source_sha256=SHA_A,
    )


def recovery(
    *,
    source_kind=VALIDATED_RUNTIME_PROVIDER,
    weight="0.30",
    directive="ACTIVE_TARGET",
):
    return RuntimeRecoveryTarget(
        asof_date="2026-08-15",
        effective_date="2026-08-14",
        target_recovery_weight=weight,
        directive=directive,
        source_kind=source_kind,
        source_id="runtime-recovery-fixture",
        source_sha256=SHA_B,
    )


def test_valid_runtime_inputs():
    x = CausalRuntimeInputs(
        asof_date="2026-08-15",
        sp2=sp2(),
        recovery=recovery(),
    )

    assert x.sp2_tickers == (
        "NVDA_US_EQ",
        "AAPL_US_EQ",
    )

    assert (
        x.recovery_target_weight
        ==
        Decimal("0.30")
    )

    assert x.sp2.runtime_eligible is True
    assert x.recovery.runtime_eligible is True


def test_historical_sp2_replay_cannot_be_current_runtime():
    with pytest.raises(
        CausalRuntimeInputError,
        match="historical SP2 replay",
    ):
        CausalRuntimeInputs(
            asof_date="2026-08-15",
            sp2=sp2(
                source_kind=
                    FROZEN_RESEARCH_REPLAY,
            ),
            recovery=recovery(),
        )


def test_historical_recovery_replay_cannot_be_current_runtime():
    with pytest.raises(
        CausalRuntimeInputError,
        match="historical recovery replay",
    ):
        CausalRuntimeInputs(
            asof_date="2026-08-15",
            sp2=sp2(),
            recovery=recovery(
                source_kind=
                    FROZEN_RESEARCH_REPLAY,
            ),
        )


def test_broker_positions_cannot_define_sp2():
    with pytest.raises(
        CausalRuntimeInputError,
        match="broker positions cannot define SP2",
    ):
        CausalRuntimeInputs(
            asof_date="2026-08-15",
            sp2=sp2(),
            recovery=recovery(),
            broker_positions_define_sp2=True,
        )


def test_duplicate_sp2_ticker_fails():
    with pytest.raises(
        CausalRuntimeInputError,
        match="distinct",
    ):
        RuntimeSP2Composition(
            asof_date="2026-08-15",
            signal_date="2026-08-01",
            effective_date="2026-08-03",
            ranked_tickers=(
                "AAPL_US_EQ",
                "AAPL_US_EQ",
            ),
            source_kind=
                VALIDATED_RUNTIME_PROVIDER,
            source_id="fixture",
            source_sha256=SHA_A,
        )


def test_future_sp2_effective_date_fails():
    with pytest.raises(
        CausalRuntimeInputError,
        match="future",
    ):
        RuntimeSP2Composition(
            asof_date="2026-08-15",
            signal_date="2026-08-01",
            effective_date="2026-08-17",
            ranked_tickers=(
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            ),
            source_kind=
                VALIDATED_RUNTIME_PROVIDER,
            source_id="fixture",
            source_sha256=SHA_A,
        )


def test_signal_after_effective_date_fails():
    with pytest.raises(
        CausalRuntimeInputError,
        match="signal_date",
    ):
        RuntimeSP2Composition(
            asof_date="2026-08-15",
            signal_date="2026-08-04",
            effective_date="2026-08-03",
            ranked_tickers=(
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            ),
            source_kind=
                VALIDATED_RUNTIME_PROVIDER,
            source_id="fixture",
            source_sha256=SHA_A,
        )


def test_arbitrary_recovery_weight_fails():
    with pytest.raises(
        CausalRuntimeInputError,
        match="frozen ladder",
    ):
        recovery(
            weight="0.35"
        )


def test_normal_requires_zero_weight():
    with pytest.raises(
        CausalRuntimeInputError,
        match="NORMAL requires zero",
    ):
        recovery(
            weight="0.10",
            directive="NORMAL",
        )


def test_active_target_requires_positive_weight():
    with pytest.raises(
        CausalRuntimeInputError,
        match="ACTIVE_TARGET requires positive",
    ):
        recovery(
            weight="0",
            directive="ACTIVE_TARGET",
        )


def test_exit_to_normal_is_zero():
    x = recovery(
        weight="0",
        directive="EXIT_TO_NORMAL",
    )

    assert (
        x.target_recovery_weight
        ==
        Decimal("0")
    )


def test_wrong_core_rule_fails():
    with pytest.raises(
        CausalRuntimeInputError,
        match="unexpected recovery rule",
    ):
        RuntimeRecoveryTarget(
            asof_date="2026-08-15",
            effective_date="2026-08-15",
            target_recovery_weight="0",
            directive="NORMAL",
            source_kind=
                VALIDATED_RUNTIME_PROVIDER,
            source_id="fixture",
            source_sha256=SHA_B,
            rule_id="SOME_OTHER_RULE",
        )


def test_bad_sha_fails():
    with pytest.raises(
        CausalRuntimeInputError,
        match="SHA-256",
    ):
        RuntimeSP2Composition(
            asof_date="2026-08-15",
            signal_date="2026-08-01",
            effective_date="2026-08-03",
            ranked_tickers=(
                "AAPL_US_EQ",
                "NVDA_US_EQ",
            ),
            source_kind=
                VALIDATED_RUNTIME_PROVIDER,
            source_id="fixture",
            source_sha256="not-a-sha",
        )


def test_asof_dates_must_match():
    r = RuntimeRecoveryTarget(
        asof_date="2026-08-14",
        effective_date="2026-08-14",
        target_recovery_weight="0",
        directive="NORMAL",
        source_kind=
            VALIDATED_RUNTIME_PROVIDER,
        source_id="fixture",
        source_sha256=SHA_B,
    )

    with pytest.raises(
        CausalRuntimeInputError,
        match="recovery input asof_date mismatch",
    ):
        CausalRuntimeInputs(
            asof_date="2026-08-15",
            sp2=sp2(),
            recovery=r,
        )


def test_global_safety_constants():
    assert CORE_RETURN_RULE_ID == (
        "SP2_RECOVERY_CORE_RETURN_D40_H378_V1"
    )

    assert LIVE_EXECUTION_AUTHORIZED is False
    assert BROKER_POST_AUTHORIZED is False
    assert BROKER_POSITIONS_CAN_DEFINE_SP2 is False
