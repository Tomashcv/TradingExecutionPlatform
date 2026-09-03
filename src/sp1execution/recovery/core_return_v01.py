from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


RULE_ID = "SP2_RECOVERY_CORE_RETURN_D40_H378_V1"

ENTRY_DELAY_US_TRADING_INTERVALS = 40
FIXED_HOLD_US_TRADING_INTERVALS = 378

HISTORICAL_RECOVERY_PROXY = "SOXX"
RECOVERY_UCITS_ISIN = "IE00BMC38736"

# Frozen C2 semantics:
#
#     drawdown = 1 - close / causal_running_ath
#
# Therefore drawdown is a POSITIVE fraction:
#
#     0.30 == 30% below ATH
#
# Ordering intentionally mirrors the frozen C2 JSON contract.
FROZEN_LADDER = (
    (0.30, 0.10),
    (0.35, 0.30),
    (0.45, 0.60),
    (0.50, 1.00),
)


class RecoveryPhase(str, Enum):
    NORMAL = "NORMAL"
    WAIT_D40 = "WAIT_D40"
    RECOVERY_ACTIVE = "RECOVERY_ACTIVE"
    OLD_ATH_GUARD = "OLD_ATH_GUARD"


class RecoveryEvent(str, Enum):
    ENTRY_OR_SCALE = "ENTRY_OR_SCALE"
    FIXED_EXIT = "FIXED_EXIT"


@dataclass(frozen=True)
class FrozenPolicy:
    rule_id: str = RULE_ID
    entry_delay_us_trading_intervals: int = ENTRY_DELAY_US_TRADING_INTERVALS
    fixed_hold_us_trading_intervals: int = FIXED_HOLD_US_TRADING_INTERVALS
    historical_proxy: str = HISTORICAL_RECOVERY_PROXY
    recovery_ucits_isin: str = RECOVERY_UCITS_ISIN


POLICY = FrozenPolicy()


def drawdown_from_close(
    close: float,
    causal_running_ath: float,
) -> float:
    """
    Frozen C2 drawdown definition:

        1 - close / causal_running_ath

    The running ATH must already include all causal observations through
    the current session. Therefore close may not materially exceed it.
    """
    px = float(close)
    ath = float(causal_running_ath)

    if px <= 0.0 or ath <= 0.0:
        raise ValueError("close and causal_running_ath must be positive")

    if px > ath + 1e-12:
        raise ValueError("close cannot exceed supplied causal running ATH")

    dd = 1.0 - px / ath

    if dd < -1e-12 or dd > 1.0 + 1e-12:
        raise ValueError("invalid drawdown fraction")

    return max(0.0, min(1.0, dd))


def target_from_drawdown(drawdown: float) -> float:
    """
    Return the exact frozen recovery-sleeve target.

    Positive drawdown convention:
        0.30 -> 0.10
        0.35 -> 0.30
        0.45 -> 0.60
        0.50 -> 1.00

    No interpolation and no optimization are permitted.
    """
    dd = float(drawdown)

    if dd < 0.0 or dd > 1.0:
        raise ValueError("drawdown must be within [0, 1]")

    target = 0.0

    for threshold, candidate in FROZEN_LADDER:
        if dd >= threshold:
            target = candidate

    return target


def validate_frozen_target(target: float) -> float:
    value = float(target)

    if value not in {0.0, 0.10, 0.30, 0.60, 1.00}:
        raise ValueError(f"non-frozen recovery target: {value}")

    return value


def session_after_intervals(
    sessions: Sequence[str],
    anchor_session: str,
    intervals: int,
) -> str:
    """
    Return the session exactly N canonical trading intervals after anchor.

    The caller must supply the actual ordered exchange calendar.
    Weekday arithmetic is intentionally forbidden here.
    """
    if intervals < 0:
        raise ValueError("intervals must be >= 0")

    calendar = tuple(sessions)

    try:
        index = calendar.index(anchor_session)
    except ValueError as exc:
        raise ValueError(
            f"anchor session absent from canonical calendar: {anchor_session}"
        ) from exc

    target_index = index + int(intervals)

    if target_index >= len(calendar):
        raise ValueError("calendar does not extend through target session")

    return calendar[target_index]


def delayed_entry_session(
    sessions: Sequence[str],
    source_execution_session: str,
) -> str:
    return session_after_intervals(
        sessions,
        source_execution_session,
        ENTRY_DELAY_US_TRADING_INTERVALS,
    )


def fixed_exit_session(
    sessions: Sequence[str],
    first_actual_entry_session: str,
) -> str:
    """
    H378 is anchored only by the FIRST actual delayed entry.

    Later scale-ups never reset the hold clock.
    """
    return session_after_intervals(
        sessions,
        first_actual_entry_session,
        FIXED_HOLD_US_TRADING_INTERVALS,
    )


def cycle_guard_released(
    *,
    fixed_exit_has_occurred: bool,
    current_close: float,
    old_ath: float,
) -> bool:
    """
    A new crash cycle may begin only when BOTH conditions are satisfied:

      1. the fixed H378 exit has occurred;
      2. the old ATH has been recovered.

    This implements max(fixed exit, old-ATH recovery).
    """
    close = float(current_close)
    ath = float(old_ath)

    if close <= 0.0 or ath <= 0.0:
        raise ValueError("prices must be positive")

    return bool(
        fixed_exit_has_occurred
        and close >= ath
    )
