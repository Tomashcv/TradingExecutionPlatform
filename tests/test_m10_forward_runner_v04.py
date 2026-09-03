from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path.home() / "Desktop/projs/SP1Execution"
RUNNER = ROOT / "scripts/sp1_demo_forward_runner_v04.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "sp1_demo_forward_runner_v04",
        RUNNER,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_window_is_new_york_causal():
    m = _module()

    # 2026-08-14 is Friday, EDT = UTC-4.
    assert not m.in_us_regular_window(datetime(2026, 8, 14, 13, 34, tzinfo=UTC))
    assert m.in_us_regular_window(datetime(2026, 8, 14, 13, 35, tzinfo=UTC))
    assert m.in_us_regular_window(datetime(2026, 8, 14, 19, 55, tzinfo=UTC))
    assert not m.in_us_regular_window(datetime(2026, 8, 14, 19, 56, tzinfo=UTC))

    # Saturday.
    assert not m.in_us_regular_window(datetime(2026, 8, 15, 14, 0, tzinfo=UTC))


def test_demo_environment_fails_closed(monkeypatch):
    m = _module()

    monkeypatch.setenv("SP1_T212_ENV", "live")
    monkeypatch.setenv("SP1_LIVE_TRADING", "false")
    with pytest.raises(
        m.ForwardSafetyError,
        match="SP1_T212_ENV=demo",
    ):
        m.validate_demo_environment()

    monkeypatch.setenv("SP1_T212_ENV", "demo")
    monkeypatch.setenv("SP1_LIVE_TRADING", "true")
    with pytest.raises(
        m.ForwardSafetyError,
        match="SP1_LIVE_TRADING=false",
    ):
        m.validate_demo_environment()


def test_cycle_lock_is_stable_per_database(tmp_path):
    m = _module()
    db = tmp_path / "state.sqlite"

    first = m.cycle_lock_path(db)
    second = m.cycle_lock_path(db)
    other = m.cycle_lock_path(tmp_path / "other.sqlite")

    assert first == second
    assert first != other
    assert "sp1execution-cycle-" in first.name


def test_dry_run_never_invokes_cycle(
    tmp_path,
    monkeypatch,
):
    m = _module()
    monkeypatch.setenv("SP1_T212_ENV", "demo")
    monkeypatch.setenv("SP1_LIVE_TRADING", "false")

    log = tmp_path / "forward.jsonl"
    rc = m.run_once(
        db_path=tmp_path / "state.sqlite",
        log_path=log,
        confirm_demo=True,
        dry_run=True,
        now=datetime(
            2026,
            8,
            14,
            14,
            0,
            tzinfo=UTC,
        ),
    )

    assert rc == 0
    text = log.read_text()
    assert "DRY_RUN_READY" in text
    assert '"confirm_demo":true' in text
