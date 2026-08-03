from __future__ import annotations

import logging
import json
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal
from pathlib import Path

import requests


@dataclass(frozen=True)
class FloodlightDecision:
    action: Literal["on", "off"]
    reason: Literal["dark", "ambient_probe"]


class FloodlightPolicy:
    """Latch darkness without mistaking the floodlight itself for daylight."""

    def __init__(
        self,
        *,
        dark_threshold: float = 0.18,
        bright_threshold: float = 0.25,
        observation_seconds: float = 10.0,
        probe_interval_seconds: float = 5 * 60,
        probe_settle_seconds: float = 4.0,
        minimum_on_seconds: float = 120.0,
        minimum_off_seconds: float = 15.0,
        minimum_samples: int = 3,
        night_dark_threshold: float | None = None,
        night_bright_threshold: float | None = None,
        night_start_hour: int = 19,
        night_end_hour: int = 6,
        wall_clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if not 0 <= dark_threshold < bright_threshold <= 1:
            raise ValueError("floodlight thresholds must satisfy dark < bright")
        if (night_dark_threshold is None) != (night_bright_threshold is None):
            raise ValueError("night floodlight thresholds must be configured together")
        if (
            night_dark_threshold is not None
            and night_bright_threshold is not None
            and not 0 <= night_dark_threshold < night_bright_threshold <= 1
        ):
            raise ValueError("night floodlight thresholds must satisfy dark < bright")
        if (
            not 0 <= night_start_hour <= 23
            or not 0 <= night_end_hour <= 23
            or night_start_hour == night_end_hour
        ):
            raise ValueError("night floodlight hours must be distinct values from 0 to 23")
        if observation_seconds <= 0:
            raise ValueError("floodlight observation period must be positive")
        if (
            probe_interval_seconds <= 0
            or probe_settle_seconds < 0
            or minimum_on_seconds < 0
            or minimum_off_seconds < 0
        ):
            raise ValueError("invalid floodlight probe timing")
        if minimum_samples < 2:
            raise ValueError("floodlight minimum_samples must be at least two")
        self.dark_threshold = dark_threshold
        self.bright_threshold = bright_threshold
        self.observation_seconds = observation_seconds
        self.probe_interval_seconds = probe_interval_seconds
        self.probe_settle_seconds = probe_settle_seconds
        self.minimum_on_seconds = minimum_on_seconds
        self.minimum_off_seconds = minimum_off_seconds
        self.minimum_samples = minimum_samples
        self.night_dark_threshold = night_dark_threshold
        self.night_bright_threshold = night_bright_threshold
        self.night_start_hour = night_start_hour
        self.night_end_hour = night_end_hour
        self._wall_clock = wall_clock
        self.desired_on = False
        self._probing = False
        self._probe_ready_at = 0.0
        self._next_probe_at = 0.0
        self._last_on_at = float("-inf")
        self._last_off_at = float("-inf")
        self._samples: deque[tuple[float, float]] = deque()
        self._sample_profile = self._threshold_profile()

    @property
    def probing(self) -> bool:
        return self._probing

    @property
    def active_thresholds(self) -> tuple[float, float]:
        """Return the active (on, off) thresholds for the local clock time."""
        if self._threshold_profile() == "night":
            return self.night_dark_threshold, self.night_bright_threshold
        return self.dark_threshold, self.bright_threshold

    def _threshold_profile(self) -> Literal["day", "night"]:
        if self.night_dark_threshold is None:
            return "day"
        hour = self._wall_clock().hour
        if self.night_start_hour < self.night_end_hour:
            is_night = self.night_start_hour <= hour < self.night_end_hour
        else:
            is_night = hour >= self.night_start_hour or hour < self.night_end_hour
        return "night" if is_night else "day"

    def _refresh_threshold_profile(self) -> None:
        profile = self._threshold_profile()
        if profile != self._sample_profile:
            # Samples from the previous lighting regime must not decide a switch.
            self._samples.clear()
            self._sample_profile = profile

    def synchronize(self, is_on: bool, *, now: float | None = None) -> None:
        """Adopt relay changes made by the device or another LAN client."""
        now = time.monotonic() if now is None else now
        if is_on:
            if not self.desired_on:
                self.desired_on = True
                self._last_on_at = now
                self._probing = False
                self._samples.clear()
                self._next_probe_at = now + self.probe_interval_seconds
        elif self.desired_on and not self._probing:
            self.desired_on = False
            self._last_off_at = now
            self._samples.clear()

    def observe(
        self, brightness: float | None, *, now: float | None = None
    ) -> FloodlightDecision | None:
        now = time.monotonic() if now is None else now
        self._refresh_threshold_profile()
        if self.desired_on:
            if now < max(
                self._next_probe_at,
                self._last_on_at + self.minimum_on_seconds,
            ):
                return None
            self.desired_on = False
            self._last_off_at = now
            self._probing = True
            self._probe_ready_at = now + self.probe_settle_seconds
            self._samples.clear()
            return FloodlightDecision("off", "ambient_probe")

        if brightness is None or now < self._probe_ready_at:
            self._samples.clear()
            return None
        value = min(1.0, max(0.0, float(brightness)))
        self._samples.append((now, value))
        cutoff = now - max(self.observation_seconds * 2, 30.0)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if (
            len(self._samples) < self.minimum_samples
            or self._samples[-1][0] - self._samples[0][0]
            < self.observation_seconds
        ):
            return None

        values = sorted(sample[1] for sample in self._samples)
        median = values[len(values) // 2]
        self._samples.clear()
        dark_threshold, bright_threshold = self.active_thresholds
        if median <= dark_threshold:
            if now < self._last_off_at + self.minimum_off_seconds:
                return None
            self.desired_on = True
            self._last_on_at = now
            self._probing = False
            self._next_probe_at = now + self.probe_interval_seconds
            return FloodlightDecision("on", "dark")
        if median >= bright_threshold:
            self._probing = False
        return None


class FloodlightClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def set(self, action: Literal["on", "off"]) -> None:
        response = self.session.post(
            f"{self.base_url}/api/lights/floodlight",
            json={"action": action},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    def get_state(self) -> bool:
        response = self.session.get(
            f"{self.base_url}/api/lights/floodlight",
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        relay = payload.get("relay2")
        if not isinstance(relay, dict):
            relay = next(
                (
                    entry
                    for entry in payload.get("relays", [])
                    if isinstance(entry, dict)
                    and entry.get("name") == "floodlight"
                ),
                None,
            )
        if not isinstance(relay, dict) or not isinstance(relay.get("on"), bool):
            raise requests.JSONDecodeError("missing floodlight relay state", "", 0)
        return relay["on"]


class FloodlightController:
    """Serialize relay writes for the automatic ambient-light policy."""

    def __init__(
        self,
        client: FloodlightClient | None,
        *,
        policy: FloodlightPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        state_path: Path | None = None,
    ) -> None:
        self.client = client
        self.policy = policy or FloodlightPolicy()
        self._sleep = sleep
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._confirmed_on = False
        self._state_path = state_path or Path(
            os.getenv(
                "CCTV_FLOODLIGHT_STATE_PATH",
                "~/.local/state/cctv/floodlight.json",
            )
        ).expanduser()
        self._worker: threading.Thread | None = None
        self._publish_state()
        if client is not None:
            self._worker = threading.Thread(
                target=self._run,
                daemon=True,
                name="cctv-floodlight",
            )
            self._worker.start()

    @classmethod
    def from_environment(cls) -> "FloodlightController":
        base_url = os.getenv("CCTV_FLOODLIGHT_BASE_URL", "").strip()
        try:
            policy = cls._policy_from_environment()
            if not base_url or "x.x" in base_url.lower():
                return cls(None, policy=policy)
            timeout = float(
                os.getenv("CCTV_FLOODLIGHT_HTTP_TIMEOUT_SECONDS", "1")
            )
            if timeout <= 0:
                raise ValueError("floodlight HTTP timeout must be positive")
            controller = cls(
                FloodlightClient(base_url, timeout_seconds=timeout),
                policy=policy,
            )
            logging.info("[floodlight] Enabled at %s.", base_url.rstrip("/"))
            return controller
        except ValueError as error:
            logging.warning(
                "[floodlight] Disabled by invalid configuration: %s", error
            )
            return cls(None)

    @staticmethod
    def _policy_from_environment() -> FloodlightPolicy:
        return FloodlightPolicy(
            dark_threshold=float(
                os.getenv("CCTV_FLOODLIGHT_DARK_THRESHOLD", "0.18")
            ),
            bright_threshold=float(
                os.getenv("CCTV_FLOODLIGHT_BRIGHT_THRESHOLD", "0.25")
            ),
            observation_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_OBSERVATION_SECONDS", "10")
            ),
            probe_interval_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_PROBE_INTERVAL_SECONDS", "300")
            ),
            probe_settle_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_PROBE_SETTLE_SECONDS", "4")
            ),
            minimum_on_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_MINIMUM_ON_SECONDS", "120")
            ),
            minimum_off_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_MINIMUM_OFF_SECONDS", "15")
            ),
            night_dark_threshold=float(
                os.getenv("CCTV_FLOODLIGHT_NIGHT_DARK_THRESHOLD", "0.15")
            ),
            night_bright_threshold=float(
                os.getenv("CCTV_FLOODLIGHT_NIGHT_BRIGHT_THRESHOLD", "0.22")
            ),
            night_start_hour=int(
                os.getenv("CCTV_FLOODLIGHT_NIGHT_START_HOUR", "19")
            ),
            night_end_hour=int(
                os.getenv("CCTV_FLOODLIGHT_NIGHT_END_HOUR", "6")
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @property
    def is_on(self) -> bool:
        """Return the last state confirmed by a successful device request."""
        with self._lock:
            return self.enabled and self._confirmed_on

    @property
    def image_adjustments_paused(self) -> bool:
        with self._lock:
            return self.enabled and self.policy.probing

    def observe(self, brightness: float | None, *, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._closed:
                return False
            decision = self.policy.observe(brightness, now=now)
            if decision is None:
                return False
            self._queue.put(("set", decision.action))
        logging.info(
            "[floodlight] Scheduling %s (%s).", decision.action, decision.reason
        )
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(("close", None))
        if self._worker is not None and threading.current_thread() is not self._worker:
            self._worker.join(timeout=2)

    def _run(self) -> None:
        while True:
            try:
                command, value = self._queue.get(timeout=5)
            except queue.Empty:
                self._refresh_state()
                continue
            if command == "close":
                return
            if command == "set":
                self._set(value)  # type: ignore[arg-type]
                continue

    def _set(self, action: Literal["on", "off"]) -> bool:
        assert self.client is not None
        for attempt in range(1, 4):
            try:
                self.client.set(action)
                with self._lock:
                    self._confirmed_on = action == "on"
                    self._publish_state()
                return True
            except requests.RequestException as error:
                logging.warning(
                    "[floodlight] %s request %d/3 failed: %s",
                    action,
                    attempt,
                    error,
                )
                if attempt < 3:
                    self._sleep(0.25 * attempt)
        return False

    def _refresh_state(self) -> None:
        assert self.client is not None
        try:
            is_on = self.client.get_state()
            if not isinstance(is_on, bool):
                raise TypeError("floodlight status must be boolean")
        except (requests.RequestException, ValueError, TypeError) as error:
            logging.warning("[floodlight] Status request failed: %s", error)
            return
        with self._lock:
            self._confirmed_on = is_on
            self.policy.synchronize(is_on)
            self._publish_state()

    def _publish_state(self) -> None:
        """Atomically expose confirmed relay state to the native HUD process."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "enabled": self.enabled,
                        "on": self.enabled and self._confirmed_on,
                        "updated_at": time.time(),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(self._state_path)
        except OSError as error:
            logging.warning("[floodlight] Unable to publish HUD state: %s", error)
