from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class ClippingResistantMetrics:
    brightness: float
    red_over_green: float | None
    blue_over_green: float | None


def clipping_resistant_metrics(frame: np.ndarray) -> ClippingResistantMetrics:
    """Measure brightness and chroma from the camera's fixed neutral wall."""
    blue = frame[..., 0].astype(np.float32)
    green = frame[..., 1].astype(np.float32)
    red = frame[..., 2].astype(np.float32)
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    unclipped = (luma > 7.0) & (luma < 248.0)
    usable_luma = luma[unclipped]
    brightness = float((usable_luma.mean() if usable_luma.size else luma.mean()) / 255.0)

    height, width = frame.shape[:2]
    x1 = int(width * 0.51)
    x2 = max(x1 + 1, int(np.ceil(width * 0.70)))
    y1 = int(height * 0.26)
    y2 = max(y1 + 1, int(np.ceil(height * 0.74)))
    reference = np.zeros((height, width), dtype=bool)
    reference[y1:y2, x1:x2] = True
    neutral_wall = reference & unclipped & (green > 7.0)
    minimum_samples = max(1, (x2 - x1) * (y2 - y1) // 20)
    if np.count_nonzero(neutral_wall) < minimum_samples:
        return ClippingResistantMetrics(brightness, None, None)
    return ClippingResistantMetrics(
        brightness,
        float(np.median(red[neutral_wall] / green[neutral_wall])),
        float(np.median(blue[neutral_wall] / green[neutral_wall])),
    )


def clipping_resistant_brightness(frame: np.ndarray) -> float:
    """Return BT.709 brightness while excluding clipped black/white pixels."""
    return clipping_resistant_metrics(frame).brightness


@dataclass(frozen=True)
class ManualExposureDecision:
    direction: str
    average_brightness: float
    shutter_lines: int
    gain_x16: int


class ManualExposureController:
    """Closed-loop manual exposure policy driven by measured frame luminance."""

    def __init__(
        self,
        *,
        target_brightness: float = 0.30,
        dim_threshold: float = 0.25,
        bright_threshold: float = 0.35,
        observation_seconds: float = 4.0,
        window_seconds: float = 12.0,
        shutter_min: int = 1,
        shutter_max: int = 1247,
        gain_min_x16: int = 16,
        gain_max_x16: int = 31,
        max_step: float = 0.50,
    ) -> None:
        if not 0 < dim_threshold < target_brightness < bright_threshold <= 1:
            raise ValueError("brightness values must satisfy dim < target < bright")
        if observation_seconds <= 0 or window_seconds < observation_seconds:
            raise ValueError("brightness window must cover the observation period")
        if not 1 <= shutter_min <= shutter_max <= 1247:
            raise ValueError("invalid shutter limits")
        if not 16 <= gain_min_x16 <= gain_max_x16 <= 496:
            raise ValueError("invalid gain limits")
        if not 0 < max_step < 1:
            raise ValueError("max_step must be between zero and one")
        self.target_brightness = target_brightness
        self.dim_threshold = dim_threshold
        self.bright_threshold = bright_threshold
        self.observation_seconds = observation_seconds
        self.window_seconds = window_seconds
        self.shutter_min = shutter_min
        self.shutter_max = shutter_max
        self.gain_min_x16 = gain_min_x16
        self.gain_max_x16 = gain_max_x16
        self.max_step = max_step
        self._profile: dict[str, Any] | None = None
        self._samples: deque[tuple[float, float]] = deque()
        self._pending = False
        self._disabled_reason: str | None = None
        self._at_limit = False
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ManualExposureController":
        try:
            return cls(
                target_brightness=float(os.getenv("CCTV_IMAGE_TARGET_BRIGHTNESS", "0.30")),
                dim_threshold=float(os.getenv("CCTV_DIM_BRIGHTNESS_THRESHOLD", "0.25")),
                bright_threshold=float(os.getenv("CCTV_BRIGHT_BRIGHTNESS_THRESHOLD", "0.35")),
                observation_seconds=float(os.getenv("CCTV_BRIGHTNESS_OBSERVATION_SECONDS", "4")),
                window_seconds=float(os.getenv("CCTV_BRIGHTNESS_WINDOW_SECONDS", "12")),
                shutter_min=int(os.getenv("CCTV_MANUAL_SHUTTER_MIN_LINES", "1")),
                shutter_max=int(os.getenv("CCTV_MANUAL_SHUTTER_MAX_LINES", "1247")),
                gain_min_x16=int(os.getenv("CCTV_MANUAL_GAIN_MIN_X16", "16")),
                gain_max_x16=int(os.getenv("CCTV_MANUAL_GAIN_MAX_X16", "31")),
                max_step=float(os.getenv("CCTV_MANUAL_EXPOSURE_MAX_STEP", "0.50")),
            )
        except (TypeError, ValueError) as error:
            controller = cls()
            controller.disable(f"invalid manual exposure configuration: {error}")
            return controller

    def initialize(self, profile: dict[str, Any]) -> dict[str, int] | None:
        """Store a frozen profile and return a normalization patch if caps require it."""
        with self._lock:
            self._validate_manual_profile(profile)
            self._profile = profile
            self._samples.clear()
            self._pending = False
            self._disabled_reason = None
            self._at_limit = False
            shutter, gain = self._values(profile)
            normalized_shutter, normalized_gain = self._normalize(shutter, gain)
            if (normalized_shutter, normalized_gain) == (shutter, gain):
                return None
            self._pending = True
            return {"shutterLines": normalized_shutter, "gainX16": normalized_gain}

    def observe(
        self, brightness: float, *, now: float | None = None
    ) -> ManualExposureDecision | None:
        now = time.monotonic() if now is None else now
        value = min(1.0, max(0.0, float(brightness)))
        with self._lock:
            if self._profile is None or self._pending or self._disabled_reason:
                return None
            if self.dim_threshold <= value <= self.bright_threshold:
                self._samples.clear()
                self._at_limit = False
                return None
            direction = "dark" if value < self.dim_threshold else "bright"
            # Switching directly between dark and bright evidence must start a fresh
            # persistence window rather than combining opposite corrections.
            if self._samples:
                previous_direction = (
                    "dark" if self._samples[-1][1] < self.dim_threshold else "bright"
                )
                if previous_direction != direction:
                    self._samples.clear()
            self._samples.append((now, value))
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if len(self._samples) < 3:
                return None
            if self._samples[-1][0] - self._samples[0][0] < self.observation_seconds:
                return None
            average = sum(sample for _, sample in self._samples) / len(self._samples)
            shutter, gain = self._values(self._profile)
            requested_shutter, requested_gain = self._correct(shutter, gain, average)
            self._samples.clear()
            if (requested_shutter, requested_gain) == (shutter, gain):
                self._at_limit = True
                return None
            self._pending = True
            self._at_limit = False
            return ManualExposureDecision(
                direction, average, requested_shutter, requested_gain
            )

    def complete(self, profile: dict[str, Any] | None, *, success: bool) -> None:
        with self._lock:
            if success and profile is not None:
                self._validate_manual_profile(profile)
                self._profile = profile
            self._pending = False
            self._samples.clear()

    def disable(self, reason: str) -> None:
        with self._lock:
            self._disabled_reason = reason
            self._pending = False
            self._samples.clear()

    def reset_observations(self) -> None:
        with self._lock:
            self._samples.clear()

    def invalidate_pending(self) -> None:
        """Discard decisions created against a camera generation that is gone."""
        with self._lock:
            self._pending = False
            self._samples.clear()

    def profile_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return self._profile

    def status_summary(self) -> str:
        with self._lock:
            if self._disabled_reason:
                return "EXPOSURE DISABLED"
            if self._profile is None:
                return "EXPOSURE --"
            shutter, gain = self._values(self._profile)
            state = "ADJUSTING" if self._pending else "MANUAL"
            if self._at_limit:
                state = "DARK LIMIT"
            white_balance = self._profile.get("whiteBalance", {})
            rgb = ""
            if all(key in white_balance for key in ("red", "green", "blue")):
                rgb = (
                    f" · WB {int(white_balance['red'])}/"
                    f"{int(white_balance['green'])}/{int(white_balance['blue'])}"
                )
            saturation = self._profile.get("color", {}).get("saturation", {})
            if all(key in saturation for key in ("u", "v")):
                rgb += f" · SAT {int(saturation['u'])}/{int(saturation['v'])}"
            return f"{state} · {shutter}L · GAIN {gain}/16 ({gain / 16:.2f}x){rgb}"

    def _correct(self, shutter: int, gain: int, brightness: float) -> tuple[int, int]:
        ratio = self.target_brightness / max(brightness, 0.001)
        ratio = min(1 + self.max_step, max(1 - self.max_step, ratio))
        target_product = shutter * gain * ratio
        if ratio >= 1:
            # In darkness, prefer a cleaner signal from a longer shutter. Add gain
            # only after the shutter range is exhausted.
            new_shutter = min(
                self.shutter_max,
                max(shutter, int(round(target_product / gain))),
            )
            new_gain = min(
                self.gain_max_x16,
                max(gain, int(round(target_product / new_shutter))),
            )
        else:
            # In bright conditions, remove noisy gain first. Shorten shutter only
            # when minimum gain cannot provide the requested reduction.
            new_gain = max(
                self.gain_min_x16,
                min(gain, int(round(target_product / shutter))),
            )
            new_shutter = max(
                self.shutter_min,
                min(shutter, int(round(target_product / new_gain))),
            )
        return new_shutter, new_gain

    def _normalize(self, shutter: int, gain: int) -> tuple[int, int]:
        product = shutter * gain
        new_shutter = min(self.shutter_max, max(self.shutter_min, shutter))
        new_gain = min(
            self.gain_max_x16,
            max(self.gain_min_x16, int(round(product / new_shutter))),
        )
        if new_gain in {self.gain_min_x16, self.gain_max_x16}:
            new_shutter = min(
                self.shutter_max,
                max(self.shutter_min, int(round(product / new_gain))),
            )
        return new_shutter, new_gain

    @staticmethod
    def _values(profile: dict[str, Any]) -> tuple[int, int]:
        exposure = profile["exposure"]
        return int(exposure["shutterLines"]), int(exposure["gainX16"])

    @staticmethod
    def _validate_manual_profile(profile: dict[str, Any]) -> None:
        exposure = profile.get("exposure")
        if not isinstance(exposure, dict):
            raise ValueError("image profile is missing exposure")
        if exposure.get("autoExposure") is not False or exposure.get("autoGain") is not False:
            raise ValueError("image profile is not manually frozen")
        int(exposure["shutterLines"])
        int(exposure["gainX16"])
