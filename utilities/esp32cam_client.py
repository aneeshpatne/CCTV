from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests.exceptions import RequestException


CAMERA_BASE_URL = os.getenv("ESP32CAM_BASE_URL", "http://192.168.0.13")
CAMERA_STREAM_URL = os.getenv("ESP32CAM_STREAM_URL", f"{CAMERA_BASE_URL}:81/stream")


@dataclass(frozen=True)
class CameraStatus:
    state: str
    source: str | None = None
    mode: str | None = None
    framesize: Any = None
    reason: str | None = None
    bad_boot_count: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def camera_online(self) -> bool:
        return self.state == "camera-online"

    @property
    def ota_only(self) -> bool:
        return self.state == "ota-only"


def _get_json(path: str, timeout: float) -> dict[str, Any] | None:
    try:
        response = requests.get(f"{CAMERA_BASE_URL}{path}", timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (RequestException, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    return data


def _status_from_payload(data: dict[str, Any], source: str) -> CameraStatus:
    mode = data.get("mode")
    if mode == "OTA":
        return CameraStatus(
            state="ota-only",
            source=source,
            mode=mode,
            reason=data.get("reason"),
            bad_boot_count=data.get("badBootCount"),
            raw=data,
        )

    return CameraStatus(
        state="camera-online",
        source=source,
        mode=mode,
        framesize=data.get("framesize"),
        reason=data.get("reason"),
        bad_boot_count=data.get("badBootCount"),
        raw=data,
    )


def get_camera_status(timeout: float = 2.0) -> CameraStatus:
    """Poll /status-v2 first, then fall back to /status."""
    v2_data = _get_json("/status-v2", timeout=timeout)
    if v2_data is not None:
        status = _status_from_payload(v2_data, "status-v2")
        if status.ota_only or status.framesize is not None:
            return status

        legacy_data = _get_json("/status", timeout=timeout)
        if legacy_data is not None and legacy_data.get("framesize") is not None:
            merged = {**legacy_data, **v2_data, "framesize": legacy_data["framesize"]}
            return _status_from_payload(merged, "status-v2+/status")

        return status

    data = _get_json("/status", timeout=timeout)
    if data is not None:
        return _status_from_payload(data, "status")

    return CameraStatus(state="offline")


def get_camera_status_with_retry(
    attempts: int = 3,
    timeout: float = 2.0,
    initial_delay: float = 0.5,
    max_delay: float = 5.0,
) -> CameraStatus:
    status = CameraStatus(state="offline")
    for attempt in range(attempts):
        status = get_camera_status(timeout=timeout)
        if status.state != "offline":
            return status
        if attempt < attempts - 1:
            time.sleep(min(max_delay, initial_delay * (2**attempt)))
    return status
