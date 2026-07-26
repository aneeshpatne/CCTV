from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Literal

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
        bright_threshold: float = 0.30,
        observation_seconds: float = 10.0,
        probe_interval_seconds: float = 15 * 60,
        probe_settle_seconds: float = 4.0,
        minimum_samples: int = 3,
    ) -> None:
        if not 0 <= dark_threshold < bright_threshold <= 1:
            raise ValueError("floodlight thresholds must satisfy dark < bright")
        if observation_seconds <= 0:
            raise ValueError("floodlight observation period must be positive")
        if probe_interval_seconds <= 0 or probe_settle_seconds < 0:
            raise ValueError("invalid floodlight probe timing")
        if minimum_samples < 2:
            raise ValueError("floodlight minimum_samples must be at least two")
        self.dark_threshold = dark_threshold
        self.bright_threshold = bright_threshold
        self.observation_seconds = observation_seconds
        self.probe_interval_seconds = probe_interval_seconds
        self.probe_settle_seconds = probe_settle_seconds
        self.minimum_samples = minimum_samples
        self.desired_on = False
        self._probing = False
        self._probe_ready_at = 0.0
        self._next_probe_at = 0.0
        self._samples: deque[tuple[float, float]] = deque()

    @property
    def probing(self) -> bool:
        return self._probing

    def observe(
        self, brightness: float | None, *, now: float | None = None
    ) -> FloodlightDecision | None:
        now = time.monotonic() if now is None else now
        if self.desired_on:
            if now < self._next_probe_at:
                return None
            self.desired_on = False
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
        if median <= self.dark_threshold:
            self.desired_on = True
            self._probing = False
            self._next_probe_at = now + self.probe_interval_seconds
            return FloodlightDecision("on", "dark")
        if median >= self.bright_threshold:
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


class FloodlightController:
    """Serialize light API writes and coalesce motion pulses."""

    _off_pattern = (("on", 0.20), ("off", 0.20), ("on", 0.20), ("off", 0.0))
    _on_pattern = (("off", 0.20), ("on", 0.20), ("off", 0.20), ("on", 0.0))

    def __init__(
        self,
        client: FloodlightClient | None,
        *,
        policy: FloodlightPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.policy = policy or FloodlightPolicy()
        self._sleep = sleep
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._lock = threading.Lock()
        self._blink_pending = False
        self._closed = False
        self._worker: threading.Thread | None = None
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
                os.getenv("CCTV_FLOODLIGHT_BRIGHT_THRESHOLD", "0.30")
            ),
            observation_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_OBSERVATION_SECONDS", "10")
            ),
            probe_interval_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_PROBE_INTERVAL_SECONDS", "900")
            ),
            probe_settle_seconds=float(
                os.getenv("CCTV_FLOODLIGHT_PROBE_SETTLE_SECONDS", "4")
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

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

    def motion_started(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._closed or self._blink_pending:
                return False
            self._blink_pending = True
            self._queue.put(("blink", None))
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
            command, value = self._queue.get()
            if command == "close":
                return
            if command == "set":
                self._set(value)  # type: ignore[arg-type]
                continue
            with self._lock:
                restore_on = self.policy.desired_on
            pattern = self._on_pattern if restore_on else self._off_pattern
            for action, delay in pattern:
                self._set(action)  # type: ignore[arg-type]
                if delay > 0:
                    self._sleep(delay)
            with self._lock:
                self._blink_pending = False

    def _set(self, action: Literal["on", "off"]) -> bool:
        assert self.client is not None
        for attempt in range(1, 4):
            try:
                self.client.set(action)
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
