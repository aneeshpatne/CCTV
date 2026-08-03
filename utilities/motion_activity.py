from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotionActivityUpdate:
    active: bool
    started: bool


class MotionActivityGuard:
    """Hold presentation state until a full quiet period has elapsed.

    Requires ``persistence_required`` *consecutive* positive frames before a new
    episode starts. Consecutive (not rolling-count) rejection kills alternating
    light-frequency flicker without delaying a real walk-through much.
    """

    def __init__(
        self,
        hold_seconds: float = 10.0,
        # Three consecutive hits ≈ 0.3–0.6s at typical ESP32-CAM FPS.
        persistence_required: int = 3,
        # Backward-compatible alias; ignored in favor of consecutive counting.
        persistence_window: int | None = None,
    ) -> None:
        self.hold_seconds = float(hold_seconds)
        self.persistence_required = max(1, int(persistence_required))
        self._consecutive_hits = 0
        self._active_until: float | None = None

    def update(self, candidate: bool, now: float) -> MotionActivityUpdate:
        if candidate:
            self._consecutive_hits += 1
        else:
            self._consecutive_hits = 0

        episode_active = (
            self._active_until is not None and now < self._active_until
        )
        # Starting a new episode needs consecutive hits. Once live, any candidate
        # extends the quiet-hold so brief detector gaps do not end real motion.
        accepted = bool(candidate) and (
            episode_active or self._consecutive_hits >= self.persistence_required
        )

        if accepted:
            started = not episode_active
            self._active_until = now + self.hold_seconds
            return MotionActivityUpdate(active=True, started=started)

        if episode_active:
            return MotionActivityUpdate(active=True, started=False)

        # Episode ended: drop the streak so a leftover hit does not immediately re-arm.
        if self._active_until is not None:
            self._active_until = None
            self._consecutive_hits = 0
        return MotionActivityUpdate(active=False, started=False)
