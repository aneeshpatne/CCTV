from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class ImageControlAPIError(RuntimeError):
    status: int | None
    code: str
    message: str
    field: str | None = None

    def __str__(self) -> str:
        location = f" ({self.field})" if self.field else ""
        status = f"HTTP {self.status} " if self.status is not None else ""
        return f"{status}{self.code}{location}: {self.message}"


class ImageControlClient:
    """Small authoritative client for the OV2640 image-control API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 2.0,
        session: requests.Session | Any | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self._sleep = sleep

    def get_profile(self) -> dict[str, Any]:
        return self._request("get", "/image-control")

    def freeze(self) -> dict[str, Any]:
        # Deliberately do not pass json= or data=: this endpoint rejects any body.
        return self._request("put", "/image-control/freeze")

    def update_exposure(self, shutter_lines: int, gain_x16: int) -> dict[str, Any]:
        return self.update_profile(
            {"exposure": {"shutterLines": shutter_lines, "gainX16": gain_x16}}
        )

    def freeze_exposure(self, profile: dict[str, Any]) -> dict[str, Any]:
        exposure = profile.get("exposure")
        if not isinstance(exposure, dict):
            raise ImageControlAPIError(None, "invalid_profile", "profile is missing exposure")
        limits = profile.get("limits", {})
        shutter_limits = limits.get("shutterLines", {})
        gain_limits = limits.get("gainX16", {})
        shutter = int(exposure["shutterLines"])
        gain = int(exposure["gainX16"])
        # Automatic loops can temporarily report values outside the range accepted
        # for manual writes (observed shutter=1247 with a documented max of 1200).
        shutter = min(int(shutter_limits.get("max", 1200)), max(int(shutter_limits.get("min", 1)), shutter))
        gain = min(int(gain_limits.get("max", 496)), max(int(gain_limits.get("min", 16)), gain))
        return self.update_exposure(
            shutter, gain
        )

    def update_profile(self, patch: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(patch, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 2048:
            raise ImageControlAPIError(
                413, "body_too_large", "serialized image-control body exceeds 2048 bytes"
            )
        return self._request("put", "/image-control", json_body=patch)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                call = getattr(self.session, method)
                kwargs: dict[str, Any] = {"timeout": self.timeout}
                if json_body is not None:
                    kwargs["json"] = json_body
                response = call(url, **kwargs)
            except requests.RequestException as exc:
                raise ImageControlAPIError(None, "network_error", str(exc)) from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise ImageControlAPIError(
                    response.status_code, "invalid_response", "response is not valid JSON"
                ) from exc

            if response.status_code == 200:
                if not isinstance(payload, dict) or payload.get("ok") is not True:
                    raise ImageControlAPIError(
                        response.status_code,
                        "invalid_response",
                        "successful response does not contain ok:true",
                    )
                return payload

            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = str(error.get("code") or "http_error")
            message = str(error.get("message") or response.reason or "request failed")
            field = error.get("field")
            if response.status_code == 503 and code == "camera_busy" and attempt < 2:
                self._sleep(0.5 * (2**attempt))
                continue
            raise ImageControlAPIError(
                response.status_code,
                code,
                message,
                str(field) if field is not None else None,
            )

        raise AssertionError("unreachable")
