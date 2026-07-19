from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


DEFAULT_STATE_PATH = Path("~/.local/state/cctv/image-control.json").expanduser()


@dataclass(frozen=True)
class WhiteBalanceDecision:
    action: Literal["adjust", "rollback"]
    average_red_over_green: float | None
    average_blue_over_green: float | None
    red: int
    green: int
    blue: int


@dataclass(frozen=True)
class _WhiteBalanceTrial:
    channel: Literal["red", "blue"]
    before_ratio: float
    before_values: tuple[int, int, int]
    candidate_values: tuple[int, int, int]


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
    """Bounded, response-verified software WB with sensor AWB disabled."""

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
        target_blue_over_green: float = 0.98,
        max_deviation_fraction: float = 0.75,
        min_response: float = 0.002,
        failure_cooldown_seconds: float = 60.0,
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
        if not 0 < max_deviation_fraction < 1:
            raise ValueError("white-balance max deviation must be between zero and one")
        if not 0 < min_response < 0.5:
            raise ValueError("white-balance minimum response must be between zero and 0.5")
        if failure_cooldown_seconds < 0:
            raise ValueError("white-balance failure cooldown must not be negative")
        self.enabled = enabled
        self.observation_seconds = observation_seconds
        self.window_seconds = window_seconds
        self.settle_seconds = settle_seconds
        self.deadband = deadband
        self.max_step = max_step
        self.target_red_over_green = target_red_over_green
        self.target_blue_over_green = target_blue_over_green
        self.max_deviation_fraction = max_deviation_fraction
        self.min_response = min_response
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self.state_store = state_store or WhiteBalanceStateStore()
        self._profile: dict[str, Any] | None = None
        self._baseline: tuple[int, int, int] | None = None
        self._verified_values: tuple[int, int, int] | None = None
        self._samples: deque[tuple[float, float, float]] = deque()
        self._trial: _WhiteBalanceTrial | None = None
        self._pending = False
        self._pending_action: Literal["adjust", "rollback"] | None = None
        self._hold_until = 0.0
        self._state = "disabled" if not enabled else "hold"
        self._disabled_reason: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ManualWhiteBalanceController":
        try:
            return cls(
                enabled=os.getenv("CCTV_AUTO_WB_ENABLED", "1").lower()
                not in {"0", "false", "no"},
                observation_seconds=float(os.getenv("CCTV_WB_OBSERVATION_SECONDS", "10")),
                window_seconds=float(os.getenv("CCTV_WB_WINDOW_SECONDS", "24")),
                settle_seconds=float(os.getenv("CCTV_WB_SETTLE_SECONDS", "8")),
                deadband=float(os.getenv("CCTV_WB_DEADBAND", "0.04")),
                max_step=int(os.getenv("CCTV_WB_MAX_STEP", "6")),
                target_red_over_green=float(
                    os.getenv("CCTV_WB_TARGET_RED_OVER_GREEN", "1.0")
                ),
                target_blue_over_green=float(
                    os.getenv("CCTV_WB_TARGET_BLUE_OVER_GREEN", "0.98")
                ),
                max_deviation_fraction=float(
                    os.getenv("CCTV_WB_MAX_DEVIATION_FRACTION", "0.75")
                ),
                min_response=float(os.getenv("CCTV_WB_MIN_RESPONSE", "0.002")),
                failure_cooldown_seconds=float(
                    os.getenv("CCTV_WB_FAILURE_COOLDOWN_SECONDS", "60")
                ),
            )
        except (TypeError, ValueError) as error:
            controller = cls(enabled=False)
            controller.disable(f"invalid white-balance configuration: {error}")
            return controller

    def saved_white_balance(self, baseline: dict[str, int]) -> dict[str, int] | None:
        baseline_values = self._dict_values(baseline)
        self._validate_channel_values(baseline_values)
        state = self.state_store.load()
        if state is None or state.get("version") != 3:
            return None
        stored_baseline = state.get("baseline")
        verified = state.get("verified")
        if not isinstance(stored_baseline, dict) or not isinstance(verified, dict):
            raise ValueError("white-balance state is missing baseline or verified values")
        if self._dict_values(stored_baseline) != baseline_values:
            return None
        values = self._dict_values(verified)
        if not self._within_bounds(values, baseline_values):
            return None
        return self._values_dict(values)

    def initialize(
        self,
        profile: dict[str, Any],
        baseline: dict[str, int] | None = None,
        *,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._validate_profile(profile)
            profile_values = self._values(profile)
            baseline_values = profile_values if baseline is None else self._dict_values(baseline)
            self._validate_channel_values(baseline_values)
            if not self._within_bounds(profile_values, baseline_values):
                raise ValueError("applied white balance is outside the calibrated safety range")
            self._profile = profile
            self._baseline = baseline_values
            self._verified_values = profile_values
            self._samples.clear()
            self._trial = None
            self._pending = False
            self._pending_action = None
            self._hold_until = now + self.settle_seconds
            if not self.enabled:
                self._state = "disabled"
                return
            self._disabled_reason = None
            self._state = "hold"
            self._save_state()

    def hold(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._samples.clear()
            self._hold_until = max(self._hold_until, now + self.settle_seconds)
            if self.enabled and not self._disabled_reason:
                self._state = "verify" if self._trial is not None else "hold"
                self._save_state()

    def observe(
        self,
        red_over_green: float | None,
        blue_over_green: float | None,
        *,
        now: float | None = None,
    ) -> WhiteBalanceDecision | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if (
                not self.enabled
                or self._disabled_reason
                or self._profile is None
                or self._baseline is None
                or self._verified_values is None
                or self._pending
            ):
                return None

            ratios = self._valid_ratios(red_over_green, blue_over_green)
            if ratios is None:
                self._samples.clear()
                if self._trial is not None and now >= self._hold_until + self.observation_seconds:
                    return self._rollback_decision(None, None)
                return None
            red_ratio, blue_ratio = ratios
            if now < self._hold_until:
                return None

            self._samples.append((now, red_ratio, blue_ratio))
            cutoff = now - self.window_seconds
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if len(self._samples) < 5:
                if self._trial is None:
                    self._state = "stable"
                return None
            if self._samples[-1][0] - self._samples[0][0] < self.observation_seconds:
                return None

            measured_red = self._median(sample[1] for sample in self._samples)
            measured_blue = self._median(sample[2] for sample in self._samples)
            self._samples.clear()

            if self._trial is not None:
                return self._verify_trial(measured_red, measured_blue)
            return self._correction_decision(measured_red, measured_blue)

    def complete(
        self,
        profile: dict[str, Any] | None,
        *,
        success: bool,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            action = self._pending_action
            if success and profile is not None:
                self._validate_profile(profile)
                values = self._values(profile)
                if self._baseline is None or not self._within_bounds(values, self._baseline):
                    raise ValueError("returned white balance is outside the calibrated safety range")
                self._profile = profile
                if action == "adjust":
                    if self._trial is None or values != self._trial.candidate_values:
                        raise ValueError("white-balance trial read-back mismatch")
                    self._state = "verify"
                    self._hold_until = now + self.settle_seconds
                elif action == "rollback":
                    if values != self._verified_values:
                        raise ValueError("white-balance rollback read-back mismatch")
                    self._trial = None
                    self._state = "guarded"
                    self._hold_until = now + self.failure_cooldown_seconds
            elif action == "adjust":
                self._trial = None
                self._state = "hold"
                self._hold_until = now + self.settle_seconds
            elif action == "rollback":
                self._trial = None
                self._state = "guarded"
                self._hold_until = now + self.failure_cooldown_seconds
            elif self.enabled and not self._disabled_reason:
                self._state = "hold"
            self._pending = False
            self._pending_action = None
            self._samples.clear()
            self._save_state()

    def sync_profile(self, profile: dict[str, Any]) -> None:
        with self._lock:
            self._validate_profile(profile)
            values = self._values(profile)
            if self._baseline is not None and not self._within_bounds(values, self._baseline):
                raise ValueError("applied white balance is outside the calibrated safety range")
            self._profile = profile
            self._save_state()

    def reset_observations(self) -> None:
        self.hold()

    def invalidate_pending(self) -> None:
        """Discard trials created against a camera generation that is gone."""
        with self._lock:
            self._pending = False
            self._pending_action = None
            self._trial = None
            self._samples.clear()
            if self.enabled and not self._disabled_reason:
                self._state = "hold"
            self._save_state()

    def disable(self, reason: str) -> None:
        with self._lock:
            self._disabled_reason = reason
            self._pending = False
            self._pending_action = None
            self._trial = None
            self._samples.clear()
            self._state = "disabled"
            self._save_state()

    def status_summary(self) -> str:
        with self._lock:
            if self._disabled_reason:
                return f"WBCTRL DISABLED ({self._disabled_reason})"
            return f"WBCTRL {self._state.upper()}"

    def _correction_decision(
        self, measured_red: float, measured_blue: float
    ) -> WhiteBalanceDecision | None:
        assert self._profile is not None
        assert self._baseline is not None
        current = self._values(self._profile)
        candidates: list[tuple[float, Literal["red", "blue"], tuple[int, int, int]]] = []
        measurements = {
            "red": (measured_red, self.target_red_over_green, 0),
            "blue": (measured_blue, self.target_blue_over_green, 2),
        }
        outside_deadband = False
        for channel, (measured, target, index) in measurements.items():
            error = target / measured
            if abs(error - 1) <= self.deadband:
                continue
            outside_deadband = True
            desired = current[index] * error
            lower, upper = self._channel_bounds(index, self._baseline)
            requested = self._bounded_step(current[index], desired, lower, upper)
            if requested == current[index]:
                continue
            values = list(current)
            values[index] = requested
            candidates.append((abs(math.log(error)), channel, tuple(values)))

        if not candidates:
            self._state = "limit" if outside_deadband else "stable"
            self._save_state()
            return None

        _, channel, candidate = max(candidates, key=lambda item: item[0])
        before_ratio = measured_red if channel == "red" else measured_blue
        self._trial = _WhiteBalanceTrial(channel, before_ratio, current, candidate)
        self._pending = True
        self._pending_action = "adjust"
        self._state = "adjust"
        self._save_state()
        return WhiteBalanceDecision(
            "adjust", measured_red, measured_blue, *candidate
        )

    def _verify_trial(
        self, measured_red: float, measured_blue: float
    ) -> WhiteBalanceDecision | None:
        assert self._trial is not None
        channel = self._trial.channel
        target = (
            self.target_red_over_green
            if channel == "red"
            else self.target_blue_over_green
        )
        measured = measured_red if channel == "red" else measured_blue
        before_error = abs(math.log(target / self._trial.before_ratio))
        after_error = abs(math.log(target / measured))
        if before_error - after_error >= self.min_response:
            assert self._profile is not None
            self._verified_values = self._values(self._profile)
            self._trial = None
            self._state = "stable"
            self._save_state()
            return None
        return self._rollback_decision(measured_red, measured_blue)

    def _rollback_decision(
        self, measured_red: float | None, measured_blue: float | None
    ) -> WhiteBalanceDecision:
        assert self._verified_values is not None
        self._pending = True
        self._pending_action = "rollback"
        self._state = "rollback"
        self._save_state()
        return WhiteBalanceDecision(
            "rollback", measured_red, measured_blue, *self._verified_values
        )

    def _bounded_step(self, current: int, desired: float, lower: int, upper: int) -> int:
        delta = round(desired) - current
        delta = min(self.max_step, max(-self.max_step, delta))
        return min(upper, max(lower, current + delta))

    def _save_state(self) -> None:
        if self._baseline is None or self._verified_values is None or self._disabled_reason:
            return
        data: dict[str, Any] = {
            "version": 3,
            "baseline": self._values_dict(self._baseline),
            "verified": self._values_dict(self._verified_values),
            "controllerState": self._state,
            "updatedAt": time.time(),
        }
        try:
            self.state_store.save(data)
        except OSError:
            pass

    def _within_bounds(
        self, values: tuple[int, int, int], baseline: tuple[int, int, int]
    ) -> bool:
        self._validate_channel_values(values)
        if values[1] != baseline[1]:
            return False
        for index in (0, 2):
            lower, upper = self._channel_bounds(index, baseline)
            if not lower <= values[index] <= upper:
                return False
        return True

    def _channel_bounds(
        self, index: int, baseline: tuple[int, int, int]
    ) -> tuple[int, int]:
        if index == 1:
            return baseline[1], baseline[1]
        value = baseline[index]
        lower = max(0, round(value * (1 - self.max_deviation_fraction)))
        upper = min(255, round(value * (1 + self.max_deviation_fraction)))
        return lower, upper

    @staticmethod
    def _median(values: Any) -> float:
        ordered = sorted(values)
        return float(ordered[len(ordered) // 2])

    @staticmethod
    def _valid_ratios(
        red_over_green: float | None, blue_over_green: float | None
    ) -> tuple[float, float] | None:
        if red_over_green is None or blue_over_green is None:
            return None
        red = float(red_over_green)
        blue = float(blue_over_green)
        if not math.isfinite(red) or not math.isfinite(blue) or red <= 0 or blue <= 0:
            return None
        return red, blue

    @staticmethod
    def _values(profile: dict[str, Any]) -> tuple[int, int, int]:
        white_balance = profile["whiteBalance"]
        return tuple(int(white_balance[name]) for name in ("red", "green", "blue"))

    @staticmethod
    def _dict_values(values: dict[str, Any]) -> tuple[int, int, int]:
        return tuple(int(values[name]) for name in ("red", "green", "blue"))

    @staticmethod
    def _values_dict(values: tuple[int, int, int]) -> dict[str, int]:
        return dict(zip(("red", "green", "blue"), values, strict=True))

    @staticmethod
    def _validate_channel_values(values: tuple[int, int, int]) -> None:
        if any(value < 0 or value > 255 for value in values):
            raise ValueError("white-balance value is out of range")

    @classmethod
    def _validate_profile(cls, profile: dict[str, Any]) -> None:
        white_balance = profile.get("whiteBalance")
        if not isinstance(white_balance, dict) or white_balance.get("auto") is not False:
            raise ValueError("image profile does not have manual white balance")
        cls._validate_channel_values(cls._values(profile))
