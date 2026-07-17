from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path("~/.local/state/cctv/image-control.json").expanduser()


@dataclass(frozen=True)
class WhiteBalanceDecision:
    average_red_over_green: float
    average_blue_over_green: float
    red: int
    green: int
    blue: int


class WhiteBalanceStateStore:
    def __init__(self, path: Path | None = None) -> None:
        configured = os.getenv("CCTV_IMAGE_CONTROL_STATE_PATH")
        self.path = Path(configured).expanduser() if configured else (path or DEFAULT_STATE_PATH)

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(data, dict):
            raise ValueError("invalid white-balance state")
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.path)


class ManualWhiteBalanceController:
    """Neutral-target software WB loop; the sensor's own AWB remains disabled."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        observation_seconds: float = 10.0,
        window_seconds: float = 24.0,
        settle_seconds: float = 8.0,
        deadband: float = 0.04,
        max_step: int = 6,
        target_red_over_green: float = 1.0,
        target_blue_over_green: float = 1.0,
        state_store: WhiteBalanceStateStore | None = None,
    ) -> None:
        if observation_seconds <= 0 or window_seconds < observation_seconds:
            raise ValueError("invalid white-balance observation window")
        if not 0 < deadband < 0.5:
            raise ValueError("white-balance deadband must be between zero and 0.5")
        if not 1 <= max_step <= 32:
            raise ValueError("white-balance max step must be between 1 and 32")
        if target_red_over_green <= 0 or target_blue_over_green <= 0:
            raise ValueError("white-balance targets must be positive")
        self.enabled = enabled
        self.observation_seconds = observation_seconds
        self.window_seconds = window_seconds
        self.settle_seconds = settle_seconds
        self.deadband = deadband
        self.max_step = max_step
        self.target_red_over_green = target_red_over_green
        self.target_blue_over_green = target_blue_over_green
        self.state_store = state_store or WhiteBalanceStateStore()
        self._profile: dict[str, Any] | None = None
        self._samples: deque[tuple[float, float, float]] = deque()
        self._pending = False
        self._hold_until = 0.0
        self._state = "disabled" if not enabled else "hold"
        self._disabled_reason: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ManualWhiteBalanceController":
        try:
            return cls(
                enabled=os.getenv("CCTV_AUTO_WB_ENABLED", "1").lower() not in {"0", "false", "no"},
                observation_seconds=float(os.getenv("CCTV_WB_OBSERVATION_SECONDS", "10")),
                window_seconds=float(os.getenv("CCTV_WB_WINDOW_SECONDS", "24")),
                settle_seconds=float(os.getenv("CCTV_WB_SETTLE_SECONDS", "8")),
                deadband=float(os.getenv("CCTV_WB_DEADBAND", "0.04")),
                max_step=int(os.getenv("CCTV_WB_MAX_STEP", "6")),
                target_red_over_green=1.0,
                target_blue_over_green=1.0,
            )
        except (TypeError, ValueError) as error:
            controller = cls(enabled=False)
            controller.disable(f"invalid white-balance configuration: {error}")
            return controller

    def saved_white_balance(self) -> dict[str, int] | None:
        state = self.state_store.load()
        if state is None:
            return None
        # Version 1 learned its target from the first scene and could persist a
        # transient disabled state. Never restore those unsafe values.
        if state.get("version") != 2:
            return None
        applied = state.get("applied")
        if not isinstance(applied, dict):
            raise ValueError("white-balance state is missing applied values")
        values = {name: int(applied[name]) for name in ("red", "green", "blue")}
        if any(value < 0 or value > 255 for value in values.values()):
            raise ValueError("saved white-balance value is out of range")
        return values

    def initialize(self, profile: dict[str, Any], *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._validate_profile(profile)
            self._profile = profile
            self._samples.clear()
            self._pending = False
            self._hold_until = now + self.settle_seconds
            if not self.enabled:
                self._state = "disabled"
                return
            # A valid authoritative profile always recovers the controller from
            # transient runtime failures. Malformed persisted state is ignored.
            self._disabled_reason = None
            self._state = "hold"
            self._save_state()

    def hold(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._samples.clear()
            self._hold_until = max(self._hold_until, now + self.settle_seconds)
            if self.enabled and not self._disabled_reason:
                self._state = "hold"
                self._save_state()

    def observe(
        self,
        red_over_green: float,
        blue_over_green: float,
        *,
        now: float | None = None,
    ) -> WhiteBalanceDecision | None:
        now = time.monotonic() if now is None else now
        red_ratio = float(red_over_green)
        blue_ratio = float(blue_over_green)
        if red_ratio <= 0 or blue_ratio <= 0:
            return None
        with self._lock:
            if (
                not self.enabled
                or self._disabled_reason
                or self._profile is None
                or self._pending
                or now < self._hold_until
            ):
                return None
            self._samples.append((now, red_ratio, blue_ratio))
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            minimum_samples = 5
            if len(self._samples) < minimum_samples:
                self._state = "stable"
                return None
            if self._samples[-1][0] - self._samples[0][0] < self.observation_seconds:
                return None
            ordered_red = sorted(sample[1] for sample in self._samples)
            ordered_blue = sorted(sample[2] for sample in self._samples)
            midpoint = len(self._samples) // 2
            measured_red = ordered_red[midpoint]
            measured_blue = ordered_blue[midpoint]
            self._samples.clear()
            target_red = self.target_red_over_green
            target_blue = self.target_blue_over_green
            red_error = target_red / measured_red
            blue_error = target_blue / measured_blue
            if abs(red_error - 1) <= self.deadband and abs(blue_error - 1) <= self.deadband:
                self._state = "stable"
                self._save_state()
                return None
            current_red, current_green, current_blue = self._values(self._profile)
            requested_red = self._bounded_step(current_red, current_red * red_error)
            requested_blue = self._bounded_step(current_blue, current_blue * blue_error)
            if (requested_red, requested_blue) == (current_red, current_blue):
                self._state = "stable"
                return None
            self._pending = True
            self._state = "adjust"
            self._save_state()
            return WhiteBalanceDecision(
                measured_red,
                measured_blue,
                requested_red,
                current_green,
                requested_blue,
            )

    def complete(
        self,
        profile: dict[str, Any] | None,
        *,
        success: bool,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if success and profile is not None:
                self._validate_profile(profile)
                self._profile = profile
                self._state = "hold"
                self._hold_until = now + self.settle_seconds
            elif self.enabled and not self._disabled_reason:
                self._state = "hold"
            self._pending = False
            self._samples.clear()
            self._save_state()

    def sync_profile(self, profile: dict[str, Any]) -> None:
        with self._lock:
            self._validate_profile(profile)
            self._profile = profile
            self._save_state()

    def reset_observations(self) -> None:
        self.hold()

    def invalidate_pending(self) -> None:
        """Discard decisions created against a camera generation that is gone."""
        with self._lock:
            self._pending = False
            self._samples.clear()
            if self.enabled and not self._disabled_reason:
                self._state = "hold"

    def disable(self, reason: str) -> None:
        with self._lock:
            self._disabled_reason = reason
            self._pending = False
            self._samples.clear()
            self._state = "disabled"
            self._save_state()

    def status_summary(self) -> str:
        with self._lock:
            if self._disabled_reason:
                return f"WBCTRL DISABLED ({self._disabled_reason})"
            return f"WBCTRL {self._state.upper()}"

    def _bounded_step(self, current: int, desired: float) -> int:
        delta = round(desired) - current
        delta = min(self.max_step, max(-self.max_step, delta))
        return min(255, max(0, current + delta))

    def _save_state(self) -> None:
        if self._profile is None:
            return
        red, green, blue = self._values(self._profile)
        if self._disabled_reason:
            return
        data: dict[str, Any] = {
            "version": 2,
            "applied": {"red": red, "green": green, "blue": blue},
            "updatedAt": time.time(),
        }
        try:
            self.state_store.save(data)
        except OSError:
            pass

    @staticmethod
    def _values(profile: dict[str, Any]) -> tuple[int, int, int]:
        white_balance = profile["whiteBalance"]
        return tuple(int(white_balance[name]) for name in ("red", "green", "blue"))

    @staticmethod
    def _validate_profile(profile: dict[str, Any]) -> None:
        white_balance = profile.get("whiteBalance")
        if not isinstance(white_balance, dict) or white_balance.get("auto") is not False:
            raise ValueError("image profile does not have manual white balance")
        for name in ("red", "green", "blue"):
            value = int(white_balance[name])
            if not 0 <= value <= 255:
                raise ValueError(f"whiteBalance.{name} is out of range")
