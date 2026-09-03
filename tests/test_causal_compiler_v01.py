from __future__ import annotations

import csv
import hashlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from sp1execution.recovery.causal_compiler_v01 import (
    RecoveryInputRow,
    assert_source_contract_compatible,
    compile_final_schedule,
    compile_guard_audit,
    compile_source_cycles,
    load_canonical_ivv_rows,
    source_events,
    trading_interval_distance,
)
from sp1execution.recovery.core_return_v01 import (
    ENTRY_DELAY_US_TRADING_INTERVALS,
    FIXED_HOLD_US_TRADING_INTERVALS,
    FROZEN_LADDER,
)
from sp1execution.strategy.robust import (
    HANDOFF_RECOVERY,
    LEVELS,
)


ROOT = Path(__file__).resolve().parents[1]

INPUT = (
    ROOT
    / "contracts/research/"
    "phase_b0_canonical_sp2_ivv_path_v0.1.csv"
)

SOURCE_ORACLE = (
    ROOT
    / "contracts/research/"
    "phase_c2_source_causal_audit_v0.1.csv"
)

HOLD_ORACLE = (
    ROOT
    / "contracts/research/"
    "phase_c2_delay_hold_audit_v0.1.csv"
)

GUARD_ORACLE = (
    ROOT
    / "contracts/research/"
    "phase_c2_guard_audit_v0.1.csv"
)

FINAL_ORACLE = (
    ROOT
    / "contracts/research/"
    "phase_c2_final_historical_schedule_v0.1.csv"
)

COMPARE_ORACLE = (
    ROOT
    / "contracts/research/"
    "phase_c2_independent_schedule_comparison_v0.1.csv"
)


HASHES = {
    INPUT:
        "8ba8a567ffc748f138a9d03d78c77a78619081d5b21aee9b6f688ae0414e03c0",

    SOURCE_ORACLE:
        "e0e029b4b8a55667712e3634f9f8d21a1c8cb16e04934ea32586baf82a3d513b",

    HOLD_ORACLE:
        "f72e6055f6c1bd07e067ca8b1745ab2138e39073b82dc0d9af42a4ab0272e69b",

    GUARD_ORACLE:
        "cc01c5905071e71e83d957cfa128129873513c10e6856e36d4836ad4306eb422",

    FINAL_ORACLE:
        "31d3ff333b772526e2c675f29a8ea1ed79b1631d8e12fa9fd21a4177f42cee62",

    COMPARE_ORACLE:
        "7c5dbf7712dd7aa779835593ef9a95208cd09f50aeec44230963f489a641f715",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def _csv(path: Path):
    with path.open(
        newline="",
        encoding="utf-8",
    ) as fh:
        return list(
            csv.DictReader(
                fh
            )
        )


def _historical_rows():
    return load_canonical_ivv_rows(
        INPUT
    )


def test_all_a3_provenance_hashes_are_exact():
    for path, expected in HASHES.items():
        assert _sha(path) == expected


def test_legacy_source_state_matches_frozen_source_contract():
    assert_source_contract_compatible()

    assert tuple(LEVELS) == FROZEN_LADDER
    assert HANDOFF_RECOVERY == pytest.approx(0.55)


def test_source_compiler_exactly_reproduces_c2_source_events():
    rows = _historical_rows()

    actual = source_events(
        compile_source_cycles(
            rows
        )
    )

    expected = _csv(
        SOURCE_ORACLE
    )

    assert len(actual) == len(expected) == 10

    for compiled, oracle in zip(
        actual,
        expected,
        strict=True,
    ):
        assert compiled.cycle_id == int(
            oracle["cycle_id"]
        )

        assert (
            compiled.event_type
            ==
            oracle["event_type"]
        )

        assert (
            compiled.signal_date
            ==
            oracle["signal_date"]
        )

        assert (
            compiled.execution_date
            ==
            oracle["execution_date"]
        )

        assert (
            compiled.old_ath_date
            ==
            oracle["old_ath_date"]
        )

        assert compiled.old_ath_value == pytest.approx(
            float(
                oracle[
                    "old_ath_value"
                ]
            ),
            abs=1e-12,
        )

        assert compiled.drawdown == pytest.approx(
            float(
                oracle[
                    "calculated_drawdown"
                ]
            ),
            abs=1e-12,
        )

        assert compiled.target_sleeve == pytest.approx(
            float(
                oracle[
                    "target_sleeve"
                ]
            ),
            abs=1e-12,
        )


def test_exact_historical_source_event_inventory():
    events = source_events(
        compile_source_cycles(
            _historical_rows()
        )
    )

    assert [
        (
            event.cycle_id,
            event.event_type,
            event.signal_date,
            event.execution_date,
            event.target_sleeve,
        )
        for event in events
    ] == [
        (
            1,
            "ENTRY_OR_SCALE",
            "2002-07-10",
            "2002-07-11",
            0.10,
        ),
        (
            1,
            "ENTRY_OR_SCALE",
            "2002-07-18",
            "2002-07-19",
            0.30,
        ),
        (
            1,
            "HANDOFF",
            "2003-12-01",
            "2003-12-02",
            0.0,
        ),
        (
            2,
            "ENTRY_OR_SCALE",
            "2008-10-06",
            "2008-10-07",
            0.10,
        ),
        (
            2,
            "ENTRY_OR_SCALE",
            "2008-10-08",
            "2008-10-09",
            0.30,
        ),
        (
            2,
            "ENTRY_OR_SCALE",
            "2008-11-19",
            "2008-11-20",
            0.60,
        ),
        (
            2,
            "ENTRY_OR_SCALE",
            "2008-11-20",
            "2008-11-21",
            1.0,
        ),
        (
            2,
            "HANDOFF",
            "2009-12-22",
            "2009-12-23",
            0.0,
        ),
        (
            3,
            "ENTRY_OR_SCALE",
            "2020-03-20",
            "2020-03-23",
            0.10,
        ),
        (
            3,
            "HANDOFF",
            "2020-04-17",
            "2020-04-20",
            0.0,
        ),
    ]


def test_final_compiler_exactly_reproduces_all_ten_c2_rows():
    actual = compile_final_schedule(
        _historical_rows()
    )

    expected = _csv(
        FINAL_ORACLE
    )

    assert len(actual) == len(expected) == 10

    for compiled, oracle in zip(
        actual,
        expected,
        strict=True,
    ):
        assert compiled.cycle_id == int(
            oracle["cycle_id"]
        )

        assert (
            compiled.old_ath_date
            ==
            oracle["old_ath_date"]
        )

        assert (
            compiled.source_signal_date
            or ""
        ) == oracle[
            "source_signal_date"
        ]

        assert (
            compiled.source_execution_t_plus_1
            or ""
        ) == oracle[
            "source_execution_t_plus_1"
        ]

        assert (
            compiled.final_execution_date
            ==
            oracle["final_execution_date"]
        )

        assert (
            compiled.event_type
            ==
            oracle["event_type"]
        )

        assert compiled.target_sleeve == pytest.approx(
            float(
                oracle[
                    "target_vvsm_weight"
                ]
            )
        )

        assert compiled.delay_td == int(
            oracle["delay_td"]
        )

        assert compiled.hold_td == int(
            oracle["hold_td"]
        )

        assert (
            compiled.fixed_exit_signal_date
            or ""
        ) == oracle[
            "fixed_exit_signal_date"
        ]


def test_exact_final_execution_inventory():
    schedule = compile_final_schedule(
        _historical_rows()
    )

    assert [
        (
            row.cycle_id,
            row.final_execution_date,
            row.event_type,
            row.target_sleeve,
        )
        for row in schedule
    ] == [
        (
            1,
            "2002-09-06",
            "DELAYED_ENTRY_OR_SCALE",
            0.10,
        ),
        (
            1,
            "2002-09-16",
            "DELAYED_ENTRY_OR_SCALE",
            0.30,
        ),
        (
            1,
            "2004-03-09",
            "FIXED_EXIT",
            0.0,
        ),
        (
            2,
            "2008-12-03",
            "DELAYED_ENTRY_OR_SCALE",
            0.10,
        ),
        (
            2,
            "2008-12-05",
            "DELAYED_ENTRY_OR_SCALE",
            0.30,
        ),
        (
            2,
            "2009-01-21",
            "DELAYED_ENTRY_OR_SCALE",
            0.60,
        ),
        (
            2,
            "2009-01-22",
            "DELAYED_ENTRY_OR_SCALE",
            1.0,
        ),
        (
            2,
            "2010-06-07",
            "FIXED_EXIT",
            0.0,
        ),
        (
            3,
            "2020-05-19",
            "DELAYED_ENTRY_OR_SCALE",
            0.10,
        ),
        (
            3,
            "2021-11-16",
            "FIXED_EXIT",
            0.0,
        ),
    ]


def test_every_entry_is_exact_d40_and_each_exit_exact_h378():
    history = _historical_rows()

    schedule = compile_final_schedule(
        history
    )

    expected = _csv(
        HOLD_ORACLE
    )

    by_key = {
        (
            row.cycle_id,
            row.source_signal_date,
        ):
            row
        for row in schedule
        if row.event_type
        ==
        "DELAYED_ENTRY_OR_SCALE"
    }

    assert len(by_key) == len(expected) == 7

    for oracle in expected:
        key = (
            int(
                oracle["cycle_id"]
            ),
            oracle[
                "source_signal_date"
            ],
        )

        actual = by_key[
            key
        ]

        assert (
            actual.source_execution_t_plus_1
            ==
            oracle[
                "source_execution_t_plus_1"
            ]
        )

        assert (
            actual.final_execution_date
            ==
            oracle[
                "delayed_execution"
            ]
        )

        assert trading_interval_distance(
            history,
            actual.source_execution_t_plus_1,
            actual.final_execution_date,
        ) == ENTRY_DELAY_US_TRADING_INTERVALS

        assert int(
            oracle[
                "delay_intervals"
            ]
        ) == ENTRY_DELAY_US_TRADING_INTERVALS

        first_entry = oracle[
            "first_actual_entry"
        ]

        fixed_exit = oracle[
            "fixed_exit"
        ]

        assert trading_interval_distance(
            history,
            first_entry,
            fixed_exit,
        ) == FIXED_HOLD_US_TRADING_INTERVALS

        assert int(
            oracle[
                "hold_intervals_from_first_actual_entry"
            ]
        ) == FIXED_HOLD_US_TRADING_INTERVALS

        assert oracle[
            "scaleup_resets_hold_clock"
        ] == "False"


def test_fixed_exit_signal_is_previous_canonical_session():
    schedule = compile_final_schedule(
        _historical_rows()
    )

    exits = [
        row
        for row in schedule
        if row.event_type == "FIXED_EXIT"
    ]

    assert [
        (
            row.final_execution_date,
            row.fixed_exit_signal_date,
        )
        for row in exits
    ] == [
        (
            "2004-03-09",
            "2004-03-08",
        ),
        (
            "2010-06-07",
            "2010-06-04",
        ),
        (
            "2021-11-16",
            "2021-11-15",
        ),
    ]


def test_guard_audit_exactly_matches_c2():
    actual = compile_guard_audit(
        _historical_rows()
    )

    expected = _csv(
        GUARD_ORACLE
    )

    assert len(actual) == len(expected) == 3

    for row, oracle in zip(
        actual,
        expected,
        strict=True,
    ):
        assert row.cycle_id == int(
            oracle["cycle_id"]
        )

        assert row.fixed_exit == oracle[
            "fixed_exit"
        ]

        assert (
            row.old_ath_recovery_date
            or ""
        ) == oracle[
            "old_ath_recovery_date"
        ]

        assert (
            row.effective_no_new_cycle_until
            or ""
        ) == oracle[
            "effective_no_new_cycle_until"
        ]

        if oracle[
            "next_cycle_id"
        ]:
            assert row.next_cycle_id == int(
                float(
                    oracle[
                        "next_cycle_id"
                    ]
                )
            )
        else:
            assert row.next_cycle_id is None

        assert (
            row.next_source_signal
            or ""
        ) == oracle[
            "next_source_signal"
        ]

        assert row.no_reentry_before_guard_release is (
            oracle[
                "no_reentry_before_guard_release"
            ]
            ==
            "True"
        )


def test_exact_guard_release_dates():
    actual = compile_guard_audit(
        _historical_rows()
    )

    assert [
        row.effective_no_new_cycle_until
        for row in actual
    ] == [
        "2005-12-13",
        "2012-08-16",
        "2021-11-16",
    ]


def test_covid_handoff_segments_source_but_does_not_cancel_core_return():
    history = _historical_rows()

    cycles = compile_source_cycles(
        history
    )

    covid = [
        cycle
        for cycle in cycles
        if cycle.cycle_id == 3
    ][0]

    assert [
        (
            event.event_type,
            event.signal_date,
        )
        for event in covid.events
    ] == [
        (
            "ENTRY_OR_SCALE",
            "2020-03-20",
        ),
        (
            "HANDOFF",
            "2020-04-17",
        ),
    ]

    schedule = [
        row
        for row in compile_final_schedule(
            history
        )
        if row.cycle_id == 3
    ]

    assert [
        (
            row.final_execution_date,
            row.event_type,
        )
        for row in schedule
    ] == [
        (
            "2020-05-19",
            "DELAYED_ENTRY_OR_SCALE",
        ),
        (
            "2021-11-16",
            "FIXED_EXIT",
        ),
    ]


def test_strict_old_ath_recovery_before_first_delayed_entry_cancels_cycle():
    start = date(
        2030,
        1,
        1,
    )

    prices = [
        100.0
        for _ in range(
            500
        )
    ]

    prices[5] = 70.0
    prices[6] = 70.0

    # 66.7% trough-to-old-ATH recovery: source handoff only.
    prices[7] = 90.0

    # Old ATH is fully recovered long before the D40 entry.
    prices[8] = 100.0

    synthetic = tuple(
        RecoveryInputRow(
            date=(
                start
                +
                timedelta(
                    days=index
                )
            ).isoformat(),
            close=price,
        )
        for index, price
        in enumerate(prices)
    )

    cycles = compile_source_cycles(
        synthetic
    )

    assert len(cycles) == 1

    assert cycles[
        0
    ].old_ath_recovery_date == (
        start
        +
        timedelta(
            days=8
        )
    ).isoformat()

    assert compile_final_schedule(
        synthetic
    ) == ()


def test_frozen_independent_comparison_oracle_is_all_true():
    rows = _csv(
        COMPARE_ORACLE
    )

    assert len(rows) == 10

    assert all(
        row["exact_match"] == "True"
        for row in rows
    )

    assert all(
        row["differences"] == ""
        for row in rows
    )
