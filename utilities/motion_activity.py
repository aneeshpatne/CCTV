from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionActivityUpdate:
    active: bool
    started: bool


class MotionActivityGuard:
    """Hold presentation state until a full quiet period has elapsed."""

    def __init__(self, hold_seconds: float = 10.0) -> None:
        self.hold_seconds = float(hold_seconds)
        self._active_until: float | None = None

    def update(self, candidate: bool, now: float) -> MotionActivityUpdate:
        if candidate:
            started = self._active_until is None or now >= self._active_until
            self._active_until = now + self.hold_seconds
            return MotionActivityUpdate(active=True, started=started)

        if self._active_until is None:
            return MotionActivityUpdate(active=False, started=False)
        if now < self._active_until:
            return MotionActivityUpdate(active=True, started=False)

        self._active_until = None
        return MotionActivityUpdate(active=False, started=False)
