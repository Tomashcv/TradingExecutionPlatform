from decimal import Decimal

import pytest


from sp1execution.recovery.causal_runtime_inputs_v01 import (
    CORE_RETURN_RULE_ID,
    VALIDATED_RUNTIME_PROVIDER,
)

from sp1execution.recovery.current_core_return_runtime_target_v01 import (
    BROKER_GET_AUTHORIZED,
    BROKER_POST_AUTHORIZED,
    DATABASE_CREATED_BY_PROVIDER,
    EXIT_TO_NORMAL_INFERRED_FROM_STATE_SNAPSHOT,
    LIVE_EXECUTION_AUTHORIZED,
    MARKET_PROVIDER_ID,
    NETWORK_PERFORMED_BY_PROVIDER,
    REPLAY_RESERVE_BUCKET_CAN_DEFINE_STRATEGY_RESERVE,
    SOURCE_PROVIDER_ID,
    CurrentCoreReturnReplayEvidence,
    CurrentCoreReturnTargetError,
    build_runtime_recovery_target_from_replay_evidence,
    evidence_sha256,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def evidence(
    **overrides,
):
    values = {
        "runtime_asof_date":
            "2026-08-15",

        "effective_date":
            "2026-08-14",

        "phase":
            "NORMAL",

        "current_target":
            "0",

        "cycle_id":
            None,

        "first_actual_entry_session":
            None,

        "fixed_exit_session":
            None,

        "old_ath":
            None,

        "old_ath_recovered":
            False,

        "open_d40_event_count":
            0,

        "state_revision":
            18,

        "current_source_cycle_count":
            3,

        "post_anchor_source_event_count":
            0,

        "current_final_event_count":
            10,

        "post_anchor_final_event_count":
            0,

        "semantic_state_deterministic":
            True,

        "pending_inventory_deterministic":
            True,

        "transition_inventory_deterministic":
            True,

        "state_machine_consistent":
            True,

        "market_provider_id":
            MARKET_PROVIDER_ID,

        "market_provider_decision_sha256":
            SHA_A,

        "stitched_series_sha256":
            SHA_B,

        "replay_decision_sha256":
            SHA_C,
    }

    values.update(
        overrides
    )

    return CurrentCoreReturnReplayEvidence(
        **values
    )


def test_observed_current_normal_snapshot_maps_to_runtime_normal_zero():
    current = evidence(
        market_provider_decision_sha256=
            "86224d72cdc5a75fb8210d0ba0fab0b3be04313e654a653d5eea0ccce7cb468c",

        stitched_series_sha256=
            "fa51c9789d8ee29484680c812bfa97b5d3502930624ecee2ca37ab03b1fad616",

        replay_decision_sha256=
            "865129b524cdf5e2bf12df01efec49df3cba6f09bc49b6f01ad64f3315eddf54",
    )

    target = (
        build_runtime_recovery_target_from_replay_evidence(
            current
        )
    )

    assert target.asof_date.isoformat() == "2026-08-15"
    assert target.effective_date.isoformat() == "2026-08-14"

    assert (
        target.target_recovery_weight
        ==
        Decimal("0")
    )

    assert target.directive == "NORMAL"

    assert (
        target.source_kind
        ==
        VALIDATED_RUNTIME_PROVIDER
    )

    assert (
        target.rule_id
        ==
        CORE_RETURN_RULE_ID
    )

    assert target.runtime_eligible is True

    assert target.source_id == (
        "CORE_RETURN_STATEFUL_REPLAY_RUNTIME_TARGET_V1:"
        "2026-08-14:NORMAL"
    )

    assert len(
        target.source_sha256
    ) == 64


def test_same_evidence_has_deterministic_hash():
    a = evidence()
    b = evidence()

    assert evidence_sha256(a) == evidence_sha256(b)

    ta = build_runtime_recovery_target_from_replay_evidence(
        a
    )

    tb = build_runtime_recovery_target_from_replay_evidence(
        b
    )

    assert ta.source_sha256 == tb.source_sha256


def test_active_recovery_maps_to_active_target():
    current = evidence(
        phase="RECOVERY_ACTIVE",
        current_target="0.30",
        cycle_id="4",
        first_actual_entry_session="2026-06-01",
        fixed_exit_session="2027-11-30",
        old_ath="10.25",
    )

    target = (
        build_runtime_recovery_target_from_replay_evidence(
            current
        )
    )

    assert (
        target.target_recovery_weight
        ==
        Decimal("0.30")
    )

    assert target.directive == "ACTIVE_TARGET"


def test_wait_d40_snapshot_remains_zero_normal_directive():
    current = evidence(
        phase="WAIT_D40",
        current_target="0",
        cycle_id="4",
        old_ath="10.25",
        open_d40_event_count=1,
    )

    target = (
        build_runtime_recovery_target_from_replay_evidence(
            current
        )
    )

    assert (
        target.target_recovery_weight
        ==
        Decimal("0")
    )

    assert target.directive == "NORMAL"


def test_old_ath_guard_snapshot_remains_zero_normal_directive():
    current = evidence(
        phase="OLD_ATH_GUARD",
        current_target="0",
        cycle_id="4",
        old_ath="10.25",
    )

    target = (
        build_runtime_recovery_target_from_replay_evidence(
            current
        )
    )

    assert (
        target.target_recovery_weight
        ==
        Decimal("0")
    )

    assert target.directive == "NORMAL"


def test_normal_nonzero_target_fails_closed():
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match="NORMAL requires zero",
    ):
        evidence(
            current_target="0.10"
        )


def test_wait_d40_without_open_event_fails_closed():
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match="requires an open D40 event",
    ):
        evidence(
            phase="WAIT_D40",
            cycle_id="4",
            old_ath="10.25",
            open_d40_event_count=0,
        )


def test_active_zero_target_fails_closed():
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match="requires positive target",
    ):
        evidence(
            phase="RECOVERY_ACTIVE",
            current_target="0",
            cycle_id="4",
            first_actual_entry_session="2026-06-01",
            fixed_exit_session="2027-11-30",
            old_ath="10.25",
        )


def test_invalid_ladder_weight_fails_closed():
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match="outside frozen recovery ladder",
    ):
        evidence(
            phase="RECOVERY_ACTIVE",
            current_target="0.35",
            cycle_id="4",
            first_actual_entry_session="2026-06-01",
            fixed_exit_session="2027-11-30",
            old_ath="10.25",
        )


def test_wrong_market_provider_fails_closed():
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match="unexpected current IVV market provider",
    ):
        evidence(
            market_provider_id="OTHER"
        )


def test_bad_sha_fails_closed():
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match="expected lowercase SHA-256",
    ):
        evidence(
            replay_decision_sha256="bad"
        )


@pytest.mark.parametrize(
    "field",
    [
        "semantic_state_deterministic",
        "pending_inventory_deterministic",
        "transition_inventory_deterministic",
        "state_machine_consistent",
    ],
)
def test_validation_gate_false_fails_closed(
    field,
):
    with pytest.raises(
        CurrentCoreReturnTargetError,
        match=f"{field} must be true",
    ):
        evidence(
            **{
                field:
                    False
            }
        )


def test_snapshot_does_not_fabricate_exit_transition():
    assert (
        EXIT_TO_NORMAL_INFERRED_FROM_STATE_SNAPSHOT
        is False
    )


def test_replay_reserve_cannot_define_operational_strategy_reserve():
    assert (
        REPLAY_RESERVE_BUCKET_CAN_DEFINE_STRATEGY_RESERVE
        is False
    )


def test_safety_constants():
    assert SOURCE_PROVIDER_ID == (
        "CORE_RETURN_STATEFUL_REPLAY_RUNTIME_TARGET_V1"
    )

    assert NETWORK_PERFORMED_BY_PROVIDER is False
    assert DATABASE_CREATED_BY_PROVIDER is False

    assert BROKER_GET_AUTHORIZED is False
    assert BROKER_POST_AUTHORIZED is False
    assert LIVE_EXECUTION_AUTHORIZED is False
