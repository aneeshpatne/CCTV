from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionActivityUpdate:
    active: bool
    started: bool


class MotionActivityGuard:
    """Hold presentation state until a full quiet period has elapsed.

    Single-frame candidates are ignored until ``persistence_required`` of the
    last ``persistence_window`` frames are positive. That rejects brief sensor
    noise and light-frequency flicker without delaying real motion much.
    """

    def __init__(
        self,
        hold_seconds: float = 10.0,
        persistence_window: int = 3,
        persistence_required: int = 2,
    ) -> None:
        self.hold_seconds = float(hold_seconds)
        window = max(1, int(persistence_window))
        required = max(1, min(int(persistence_required), window))
        self.persistence_required = required
        self._recent = [False] * window
        self._recent_index = 0
        self._active_until: float | None = None

    def update(self, candidate: bool, now: float) -> MotionActivityUpdate:
        self._recent[self._recent_index] = bool(candidate)
        self._recent_index = (self._recent_index + 1) % len(self._recent)
        # Current frame must itself be a candidate so quiet frames never extend
        # the hold from leftover positives in the rolling window.
        accepted = bool(candidate) and (
            sum(1 for value in self._recent if value) >= self.persistence_required
        )

        if accepted:
            started = self._active_until is None or now >= self._active_until
            self._active_until = now + self.hold_seconds
            return MotionActivityUpdate(active=True, started=started)

        if self._active_until is None:
            return MotionActivityUpdate(active=False, started=False)
        if now < self._active_until:
            return MotionActivityUpdate(active=True, started=False)

        self._active_until = None
        return MotionActivityUpdate(active=False, started=False)
