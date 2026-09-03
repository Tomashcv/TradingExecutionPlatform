from pathlib import Path
import csv
import hashlib
import json

import pytest

from sp1execution.recovery.core_return_v01 import (
    ENTRY_DELAY_US_TRADING_INTERVALS,
    FIXED_HOLD_US_TRADING_INTERVALS,
    FROZEN_LADDER,
    HISTORICAL_RECOVERY_PROXY,
    RECOVERY_UCITS_ISIN,
    RULE_ID,
    cycle_guard_released,
    delayed_entry_session,
    drawdown_from_close,
    fixed_exit_session,
    target_from_drawdown,
    validate_frozen_target,
)


ROOT = Path(__file__).resolve().parents[1]

RULE = (
    ROOT
    / "contracts/research/phase_c2_final_rule_spec_v0.1.json"
)

SCHEDULE = (
    ROOT
    / "contracts/research/phase_c2_final_historical_schedule_v0.1.csv"
)

RULE_SHA = (
    "9b992766f2c99028a3004a67d8850e2a91f4861495cdc731ddf272f8d608de61"
)

SCHEDULE_SHA = (
    "31d3ff333b772526e2c675f29a8ea1ed79b1631d8e12fa9fd21a4177f42cee62"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_research_artifacts_are_byte_identical():
    assert sha256(RULE) == RULE_SHA
    assert sha256(SCHEDULE) == SCHEDULE_SHA


def test_module_semantics_are_derived_directly_from_frozen_rule_json():
    obj = json.loads(RULE.read_text())

    assert (
        obj["crash_reference"]["drawdown_definition"]
        ==
        "1 - close / causal_running_ath"
    )

    expected_ladder = tuple(
        (
            float(row["drawdown"]),
            float(row["recovery_sleeve_target"]),
        )
        for row in obj["trigger_ladder"]
    )

    assert FROZEN_LADDER == expected_ladder

    assert (
        ENTRY_DELAY_US_TRADING_INTERVALS
        ==
        int(
            obj["signal_execution_semantics"][
                "entry_or_scale_delay_us_trading_intervals"
            ]
        )
    )

    assert (
        FIXED_HOLD_US_TRADING_INTERVALS
        ==
        int(
            obj["hold_semantics"][
                "fixed_hold_us_trading_intervals"
            ]
        )
    )


def test_frozen_policy_identity():
    assert RULE_ID == "SP2_RECOVERY_CORE_RETURN_D40_H378_V1"
    assert ENTRY_DELAY_US_TRADING_INTERVALS == 40
    assert FIXED_HOLD_US_TRADING_INTERVALS == 378
    assert HISTORICAL_RECOVERY_PROXY == "SOXX"
    assert RECOVERY_UCITS_ISIN == "IE00BMC38736"


@pytest.mark.parametrize(
    ("close", "ath", "expected"),
    [
        (100.0, 100.0, 0.0),
        (70.0, 100.0, 0.30),
        (65.0, 100.0, 0.35),
        (55.0, 100.0, 0.45),
        (50.0, 100.0, 0.50),
    ],
)
def test_drawdown_definition_is_positive_fraction(close, ath, expected):
    assert drawdown_from_close(close, ath) == pytest.approx(expected)


def test_close_above_supplied_running_ath_fails_closed():
    with pytest.raises(ValueError):
        drawdown_from_close(101.0, 100.0)


@pytest.mark.parametrize(
    ("drawdown", "expected"),
    [
        (0.0, 0.0),
        (0.299999, 0.0),
        (0.30, 0.10),
        (0.349999, 0.10),
        (0.35, 0.30),
        (0.449999, 0.30),
        (0.45, 0.60),
        (0.499999, 0.60),
        (0.50, 1.00),
        (0.75, 1.00),
    ],
)
def test_exact_positive_frozen_ladder(drawdown, expected):
    assert target_from_drawdown(drawdown) == expected


@pytest.mark.parametrize(
    "drawdown",
    [-0.30, -0.01, 1.01],
)
def test_non_contract_drawdown_domain_fails_closed(drawdown):
    with pytest.raises(ValueError):
        target_from_drawdown(drawdown)


@pytest.mark.parametrize(
    "target",
    [0.0, 0.10, 0.30, 0.60, 1.00],
)
def test_only_frozen_targets_are_accepted(target):
    assert validate_frozen_target(target) == target


@pytest.mark.parametrize(
    "target",
    [0.05, 0.20, 0.50, 0.90],
)
def test_non_frozen_targets_fail_closed(target):
    with pytest.raises(ValueError):
        validate_frozen_target(target)


def test_d40_means_exactly_40_canonical_sessions():
    sessions = [f"S{i:03d}" for i in range(500)]

    assert delayed_entry_session(
        sessions,
        "S010",
    ) == "S050"


def test_h378_is_anchored_to_first_actual_entry():
    sessions = [f"S{i:03d}" for i in range(500)]

    assert fixed_exit_session(
        sessions,
        "S010",
    ) == "S388"

    assert fixed_exit_session(
        sessions,
        "S010",
    ) != "S428"


def test_guard_requires_exit_and_old_ath_recovery():
    assert not cycle_guard_released(
        fixed_exit_has_occurred=False,
        current_close=101.0,
        old_ath=100.0,
    )

    assert not cycle_guard_released(
        fixed_exit_has_occurred=True,
        current_close=99.99,
        old_ath=100.0,
    )

    assert cycle_guard_released(
        fixed_exit_has_occurred=True,
        current_close=100.0,
        old_ath=100.0,
    )


def test_c2_schedule_has_exactly_ten_execution_events():
    with SCHEDULE.open(newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 10


def test_c2_schedule_contains_exact_frozen_execution_dates():
    text = SCHEDULE.read_text()

    expected_dates = [
        "2002-09-06",
        "2002-09-16",
        "2004-03-09",
        "2008-12-03",
        "2008-12-05",
        "2009-01-21",
        "2009-01-22",
        "2010-06-07",
        "2020-05-19",
        "2021-11-16",
    ]

    for date in expected_dates:
        assert date in text
