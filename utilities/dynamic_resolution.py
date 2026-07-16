from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolutionDecision:
    framesize: int
    average_brightness: float


class DynamicResolutionController:
    """Choose an ESP32 framesize from sustained, normalized scene brightness."""

    def __init__(
        self,
        *,
        initial_framesize: int = 12,
        dim_threshold: float = 0.25,
        bright_threshold: float = 0.35,
        observation_seconds: float = 30.0,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 15 * 60.0,
    ) -> None:
        if not 0 <= dim_threshold < bright_threshold <= 1:
            raise ValueError("brightness thresholds must satisfy 0 <= dim < bright <= 1")
        if observation_seconds <= 0 or window_seconds < observation_seconds:
            raise ValueError("brightness window must cover the observation period")
        if cooldown_seconds < 0:
            raise ValueError("resolution cooldown cannot be negative")

        self.dim_threshold = dim_threshold
        self.bright_threshold = bright_threshold
        self.observation_seconds = observation_seconds
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._selected_framesize = initial_framesize
        self._pending_framesize: int | None = None
        self._last_change_at: float | None = None
        self._samples: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, *, initial_framesize: int = 12) -> "DynamicResolutionController":
        return cls(
            initial_framesize=initial_framesize,
            dim_threshold=float(os.getenv("CCTV_DIM_BRIGHTNESS_THRESHOLD", "0.25")),
            bright_threshold=float(os.getenv("CCTV_BRIGHT_BRIGHTNESS_THRESHOLD", "0.35")),
            observation_seconds=float(os.getenv("CCTV_BRIGHTNESS_OBSERVATION_SECONDS", "30")),
            window_seconds=float(os.getenv("CCTV_BRIGHTNESS_WINDOW_SECONDS", "60")),
            cooldown_seconds=float(os.getenv("CCTV_RESOLUTION_COOLDOWN_SECONDS", "900")),
        )

    @property
    def selected_framesize(self) -> int:
        """Return the requested target, including a change currently in progress."""
        with self._lock:
            return self._pending_framesize or self._selected_framesize

    def observe(self, brightness: float, *, now: float | None = None) -> ResolutionDecision | None:
        now = time.monotonic() if now is None else now
        brightness = min(1.0, max(0.0, float(brightness)))

        with self._lock:
            self._samples.append((now, brightness))
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

            if self._pending_framesize is not None or len(self._samples) < 2:
                return None
            if self._samples[-1][0] - self._samples[0][0] < self.observation_seconds:
                return None
            if self._last_change_at is not None and now - self._last_change_at < self.cooldown_seconds:
                return None

            average = sum(value for _, value in self._samples) / len(self._samples)
            target: int | None = None
            if average < self.dim_threshold:
                target = 11
            elif average > self.bright_threshold:
                target = 12

            if target is None or target == self._selected_framesize:
                return None

            self._pending_framesize = target
            return ResolutionDecision(framesize=target, average_brightness=average)

    def complete_change(self, framesize: int, *, success: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._pending_framesize != framesize:
                return
            self._pending_framesize = None
            if success:
                self._selected_framesize = framesize
                self._last_change_at = now
                self._samples.clear()

    def reset_observations(self) -> None:
        """Require a fresh observation period after a signal interruption."""
        with self._lock:
            self._samples.clear()

