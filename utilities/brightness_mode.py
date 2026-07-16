from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal


BrightnessMode = Literal["bright", "dark"]


@dataclass(frozen=True)
class BrightnessModeDecision:
    mode: BrightnessMode
    average_brightness: float


class BrightnessModeController:
    """Choose one reset-backed AGC mode from sustained scene brightness."""

    def __init__(
        self,
        *,
        initial_mode: BrightnessMode = "bright",
        dim_threshold: float = 0.25,
        bright_threshold: float = 0.35,
        observation_seconds: float = 30.0,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 15 * 60.0,
    ) -> None:
        if initial_mode not in {"bright", "dark"}:
            raise ValueError("initial brightness mode must be bright or dark")
        if not 0 <= dim_threshold < bright_threshold <= 1:
            raise ValueError("brightness thresholds must satisfy 0 <= dim < bright <= 1")
        if observation_seconds <= 0 or window_seconds < observation_seconds:
            raise ValueError("brightness window must cover the observation period")
        if cooldown_seconds < 0:
            raise ValueError("brightness reset cooldown cannot be negative")

        self.dim_threshold = dim_threshold
        self.bright_threshold = bright_threshold
        self.observation_seconds = observation_seconds
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._selected_mode = initial_mode
        self._pending_mode: BrightnessMode | None = None
        self._last_change_at: float | None = None
        self._samples: deque[tuple[float, float]] = deque()
        self._synchronized = False
        self._lock = threading.Lock()

    @classmethod
    def from_environment(
        cls, *, initial_mode: BrightnessMode = "bright"
    ) -> "BrightnessModeController":
        cooldown = os.getenv(
            "CCTV_BRIGHTNESS_RESET_COOLDOWN_SECONDS",
            os.getenv("CCTV_RESOLUTION_COOLDOWN_SECONDS", "900"),
        )
        return cls(
            initial_mode=initial_mode,
            dim_threshold=float(os.getenv("CCTV_DIM_BRIGHTNESS_THRESHOLD", "0.25")),
            bright_threshold=float(os.getenv("CCTV_BRIGHT_BRIGHTNESS_THRESHOLD", "0.35")),
            observation_seconds=float(
                os.getenv("CCTV_BRIGHTNESS_OBSERVATION_SECONDS", "30")
            ),
            window_seconds=float(os.getenv("CCTV_BRIGHTNESS_WINDOW_SECONDS", "60")),
            cooldown_seconds=float(cooldown),
        )

    @property
    def selected_mode(self) -> BrightnessMode:
        """Return the desired mode, including a reset currently in progress."""
        with self._lock:
            return self._pending_mode or self._selected_mode

    @property
    def selected_agc(self) -> int:
        return 1 if self.selected_mode == "dark" else 0

    def synchronize_from_agc(self, agc: object) -> None:
        """Initialize mode once from camera status without overriding a transition."""
        try:
            value = int(agc)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if value not in {0, 1}:
            return

        with self._lock:
            if self._synchronized or self._pending_mode is not None:
                return
            self._selected_mode = "dark" if value == 1 else "bright"
            self._synchronized = True

    def observe(
        self, brightness: float, *, now: float | None = None
    ) -> BrightnessModeDecision | None:
        now = time.monotonic() if now is None else now
        brightness = min(1.0, max(0.0, float(brightness)))

        with self._lock:
            self._samples.append((now, brightness))
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

            if self._pending_mode is not None or len(self._samples) < 2:
                return None
            if self._samples[-1][0] - self._samples[0][0] < self.observation_seconds:
                return None
            if self._last_change_at is not None and now - self._last_change_at < self.cooldown_seconds:
                return None

            average = sum(value for _, value in self._samples) / len(self._samples)
            target: BrightnessMode | None = None
            if average < self.dim_threshold:
                target = "dark"
            elif average > self.bright_threshold:
                target = "bright"

            if target is None or target == self._selected_mode:
                return None

            self._pending_mode = target
            return BrightnessModeDecision(mode=target, average_brightness=average)

    def complete_transition(
        self,
        mode: BrightnessMode,
        *,
        success: bool,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._pending_mode != mode:
                return
            self._pending_mode = None
            self._samples.clear()
            if success:
                self._selected_mode = mode
                self._last_change_at = now
                self._synchronized = True

    def reset_observations(self) -> None:
        """Require fresh brightness evidence after a signal interruption."""
        with self._lock:
            self._samples.clear()
