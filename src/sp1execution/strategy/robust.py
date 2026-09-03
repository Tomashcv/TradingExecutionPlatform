from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

LEVELS = (
    (0.30, 0.10),
    (0.35, 0.30),
    (0.45, 0.60),
    (0.50, 1.00),
)
HANDOFF_RECOVERY = 0.55


class Mode(str, Enum):
    NORMAL = "NORMAL"
    CRASH = "CRASH"
    POST_HANDOFF = "POST_HANDOFF"


@dataclass(frozen=True)
class Decision:
    mode: Mode
    target_sp500: float
    target_sp2: float
    event: str


def drawdown_fraction(price: float, old_peak: float) -> float:
    if old_peak <= 0 or price <= 0:
        raise ValueError("price and old_peak must be positive")
    return max(0.0, 1.0 - price / old_peak)


def recovery_fraction(price: float, old_peak: float, trough: float) -> float:
    if not (0 < trough <= old_peak) or price <= 0:
        raise ValueError("Require 0 < trough <= old_peak and price > 0")
    denominator = old_peak - trough
    if denominator == 0:
        return 0.0
    return (price - trough) / denominator


def crash_target(drawdown: float) -> float:
    target = 0.0
    for threshold, target_sp500 in LEVELS:
        if drawdown >= threshold:
            target = target_sp500
    return target


@dataclass
class RobustState:
    mode: Mode = Mode.NORMAL
    old_peak: float | None = None
    trough: float | None = None
    target_sp500: float = 0.0

    def observe_close(self, price: float) -> Decision:
        if price <= 0:
            raise ValueError("price must be positive")

        if self.old_peak is None:
            self.old_peak = price

        if self.mode == Mode.NORMAL:
            self.old_peak = max(price, self.old_peak)
            dd = drawdown_fraction(price, self.old_peak)
            new_target = crash_target(dd)
            if new_target > 0:
                self.mode = Mode.CRASH
                self.trough = price
                self.target_sp500 = new_target
                return Decision(
                    self.mode,
                    self.target_sp500,
                    1.0 - self.target_sp500,
                    f"ROTATE_DD_{100 * dd:.2f}",
                )
            return Decision(self.mode, 0.0, 1.0, "NO_ACTION")

        if self.mode == Mode.CRASH:
            assert self.trough is not None
            self.trough = min(self.trough, price)

            dd = drawdown_fraction(price, self.old_peak)
            new_target = crash_target(dd)
            if new_target > self.target_sp500:
                self.target_sp500 = new_target
                return Decision(
                    self.mode,
                    self.target_sp500,
                    1.0 - self.target_sp500,
                    f"ROTATE_DD_{100 * dd:.2f}",
                )

            rec = recovery_fraction(price, self.old_peak, self.trough)
            if rec >= HANDOFF_RECOVERY:
                self.mode = Mode.POST_HANDOFF
                self.target_sp500 = 0.0
                return Decision(
                    self.mode,
                    0.0,
                    1.0,
                    f"HANDOFF_REC_{100 * rec:.2f}",
                )

            return Decision(
                self.mode,
                self.target_sp500,
                1.0 - self.target_sp500,
                "NO_ACTION",
            )

        if price >= self.old_peak:
            self.mode = Mode.NORMAL
            self.old_peak = price
            self.trough = None
            self.target_sp500 = 0.0
            return Decision(self.mode, 0.0, 1.0, "REARM_AFTER_OLD_ATH")

        return Decision(self.mode, 0.0, 1.0, "NO_ACTION")
