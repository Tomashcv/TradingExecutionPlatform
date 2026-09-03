from types import SimpleNamespace

from sp1execution.engine.strategy_engine import (
    event_type,
    replay_robust,
    target_mix_for_event,
)


def test_true_hold_same_membership_same_overlay_no_trade():
    assert (
        event_type(
            previous_membership=("AAPL", "NVDA"),
            current_membership=("NVDA", "AAPL"),
            previous_overlay=0.0,
            current_overlay=0.0,
        )
        == "NO_TRADE_TRUE_HOLD"
    )


def test_membership_change_forces_half_half_inside_sp2():
    event = event_type(
        previous_membership=("MSFT", "AAPL"),
        current_membership=("AAPL", "NVDA"),
        previous_overlay=0.3,
        current_overlay=0.3,
    )
    targets = target_mix_for_event(
        event=event,
        membership=("AAPL", "NVDA"),
        overlay=0.3,
        current_values_eur={"AAPL": 100, "NVDA": 0},
        previous_mix=None,
    )
    assert targets == {"AAPL": 0.35, "NVDA": 0.35, "VUAA": 0.3}


def test_overlay_change_preserves_sp2_drift():
    targets = target_mix_for_event(
        event="ROBUST_OVERLAY_CHANGE",
        membership=("AAPL", "NVDA"),
        overlay=0.3,
        current_values_eur={"AAPL": 600.0, "NVDA": 400.0},
        previous_mix=None,
    )
    assert abs(targets["AAPL"] - 0.42) < 1e-12
    assert abs(targets["NVDA"] - 0.28) < 1e-12
    assert abs(targets["VUAA"] - 0.30) < 1e-12


def test_robust_threshold_and_handoff_replay():
    rows = [
        SimpleNamespace(close=100.0, date="2026-01-01"),
        SimpleNamespace(close=70.0, date="2026-01-02"),
        SimpleNamespace(close=65.0, date="2026-01-03"),
        SimpleNamespace(close=50.0, date="2026-01-04"),
        SimpleNamespace(close=77.5, date="2026-01-05"),
    ]
    status = replay_robust(rows)
    assert status.target_sp500 == 0.0
    assert status.mode == "POST_HANDOFF"
