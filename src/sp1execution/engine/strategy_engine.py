from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sp1execution.strategy.robust import RobustState, drawdown_fraction, recovery_fraction


@dataclass(frozen=True)
class OverlayStatus:
    as_of: str
    ivv_close: float
    mode: str
    old_peak: float
    trough: float | None
    drawdown: float
    recovery: float | None
    target_sp500: float
    last_event: str


def replay_robust(rows) -> OverlayStatus:
    state = RobustState()
    last_event = "NO_ACTION"
    last_row = None

    for row in rows:
        last_row = row
        decision = state.observe_close(row.close)
        if decision.event != "NO_ACTION":
            last_event = decision.event

    if last_row is None or state.old_peak is None:
        raise RuntimeError("Cannot replay ROBUST without IVV closes.")

    dd = drawdown_fraction(last_row.close, state.old_peak)
    rec = None
    if state.trough is not None:
        rec = recovery_fraction(last_row.close, state.old_peak, state.trough)

    return OverlayStatus(
        as_of=last_row.date,
        ivv_close=last_row.close,
        mode=state.mode.value,
        old_peak=state.old_peak,
        trough=state.trough,
        drawdown=dd,
        recovery=rec,
        target_sp500=state.target_sp500,
        last_event=last_event,
    )


def decision_id(
    *,
    membership_month: str,
    symbols: tuple[str, str],
    overlay: OverlayStatus,
    initial_capital_eur: float | None = None,
    schema_version: str = "0.3.3q1",
) -> str:
    payload = {
        "schema_version": schema_version,
        "membership_month": membership_month,
        "symbols": sorted(symbols),
        "overlay_as_of": overlay.as_of,
        "overlay_target": overlay.target_sp500,
        "overlay_mode": overlay.mode,
        "initial_capital_eur": initial_capital_eur,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return f"{overlay.as_of}_{membership_month}_{digest}"


def event_type(
    *,
    previous_membership: tuple[str, str] | None,
    current_membership: tuple[str, str],
    previous_overlay: float | None,
    current_overlay: float,
) -> str:
    if previous_membership is None or previous_overlay is None:
        return "BOOTSTRAP_INITIAL_ALLOCATION"
    if set(previous_membership) != set(current_membership):
        return "MONTHLY_MEMBERSHIP_CHANGE"
    if abs(previous_overlay - current_overlay) > 1e-12:
        return "ROBUST_OVERLAY_CHANGE"
    return "NO_TRADE_TRUE_HOLD"


def target_mix_for_event(
    *,
    event: str,
    membership: tuple[str, str],
    overlay: float,
    current_values_eur: dict[str, float],
    previous_mix: dict[str, float] | None,
) -> dict[str, float]:
    sp2_weight = 1.0 - overlay

    if event in {"BOOTSTRAP_INITIAL_ALLOCATION", "MONTHLY_MEMBERSHIP_CHANGE"}:
        mix = {membership[0]: 0.5, membership[1]: 0.5}
    elif event == "ROBUST_OVERLAY_CHANGE":
        current_sp2 = {
            symbol: max(0.0, current_values_eur.get(symbol, 0.0))
            for symbol in membership
        }
        total = sum(current_sp2.values())
        if total > 1e-9:
            mix = {symbol: current_sp2[symbol] / total for symbol in membership}
        elif previous_mix and set(previous_mix) == set(membership):
            mix = {symbol: float(previous_mix[symbol]) for symbol in membership}
        else:
            mix = {membership[0]: 0.5, membership[1]: 0.5}
    else:
        return {}

    return {
        membership[0]: sp2_weight * mix[membership[0]],
        membership[1]: sp2_weight * mix[membership[1]],
        "VUAA": overlay,
    }


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()
