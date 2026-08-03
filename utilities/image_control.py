from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests


DEFAULT_STATS_ROI = {
    "x": 0.51,
    "y": 0.26,
    "w": 0.19,
    "h": 0.48,
}


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


@dataclass
class CameraCapabilities:
    """Feature flags discovered from the live camera firmware."""

    awb_truthful_fields: bool = False
    awb_freeze: bool = False
    image_stats: bool = False
    image_stats_roi: bool = False
    raw_stats: bool = False
    capture_minimal: bool = False
    probed: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "awb_truthful_fields": self.awb_truthful_fields,
            "awb_freeze": self.awb_freeze,
            "image_stats": self.image_stats,
            "image_stats_roi": self.image_stats_roi,
            "raw_stats": self.raw_stats,
            "capture_minimal": self.capture_minimal,
            "probed": self.probed,
        }


@dataclass(frozen=True)
class ImageStatsRoi:
    x: float
    y: float
    w: float
    h: float
    samples: int
    mean_r: float
    mean_g: float
    mean_b: float
    median_rg: float | None
    median_bg: float | None
    usable: bool


@dataclass(frozen=True)
class ImageStats:
    ok: bool
    sensor: str | None
    timestamp_ms: int | None
    domain: str | None
    mean_y: float | None
    clip_black_frac: float | None
    clip_white_frac: float | None
    white_balance: dict[str, Any]
    roi: ImageStatsRoi | None
    raw: dict[str, Any] = field(repr=False)


class ImageControlClient:
    """Authoritative client for OV2640 image-control + stats firmware APIs."""

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
        self.capabilities = CameraCapabilities()

    def get_profile(self) -> dict[str, Any]:
        return self._request("get", "/image-control")

    def freeze(self) -> dict[str, Any]:
        # Deliberately do not pass json= or data=: this endpoint rejects any body.
        return self._request("put", "/image-control/freeze")

    def freeze_awb(self, *, timeout: float | None = 15.0) -> dict[str, Any]:
        """One-shot AWB settle → manual RGB latch (AE/AGC untouched)."""
        return self._request(
            "put",
            "/image-control/awb/freeze",
            timeout=timeout if timeout is not None else self.timeout,
        )

    def recalibrate_v3(self, *, timeout: float | None = 30.0) -> dict[str, Any]:
        """Run firmware recalibrate-v3 (full image-control recalibration pass)."""
        # Deliberately do not pass json= or data=: this endpoint rejects any body.
        return self._request(
            "put",
            "/image-control/recalibrate-v3",
            timeout=timeout if timeout is not None else self.timeout,
        )

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
        # for manual writes. Always honor the authoritative limit advertised by
        # the camera, with the current OV2640 maximum as the compatibility fallback.
        shutter = min(int(shutter_limits.get("max", 1247)), max(int(shutter_limits.get("min", 1)), shutter))
        gain = min(int(gain_limits.get("max", 496)), max(int(gain_limits.get("min", 16)), gain))
        return self.update_exposure(shutter, gain)

    def update_profile(self, patch: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(patch, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 2048:
            raise ImageControlAPIError(
                413, "body_too_large", "serialized image-control body exceeds 2048 bytes"
            )
        return self._request("put", "/image-control", json_body=patch)

    def set_white_balance_auto(self, enabled: bool) -> dict[str, Any]:
        return self.update_profile({"whiteBalance": {"auto": bool(enabled)}})

    def set_white_balance_manual(self, red: int, green: int, blue: int) -> dict[str, Any]:
        return self.update_profile(
            {
                "whiteBalance": {
                    "auto": False,
                    "red": int(red),
                    "green": int(green),
                    "blue": int(blue),
                }
            }
        )

    def get_image_stats(self, *, timeout: float | None = None) -> ImageStats:
        payload = self._request(
            "get",
            "/image-stats",
            timeout=timeout if timeout is not None else self.timeout,
            require_ok=True,
        )
        return self._parse_image_stats(payload)

    def get_image_stats_roi(self) -> dict[str, float]:
        payload = self._request("get", "/image-stats/roi")
        normalized = payload.get("normalized") or {}
        return {
            "x": float(normalized["x"]),
            "y": float(normalized["y"]),
            "w": float(normalized["w"]),
            "h": float(normalized["h"]),
        }

    def set_image_stats_roi(
        self,
        *,
        x: float = DEFAULT_STATS_ROI["x"],
        y: float = DEFAULT_STATS_ROI["y"],
        w: float = DEFAULT_STATS_ROI["w"],
        h: float = DEFAULT_STATS_ROI["h"],
    ) -> dict[str, float]:
        payload = self._request(
            "put",
            "/image-stats/roi",
            json_body={"normalized": {"x": float(x), "y": float(y), "w": float(w), "h": float(h)}},
        )
        normalized = payload.get("normalized") or {}
        return {
            "x": float(normalized["x"]),
            "y": float(normalized["y"]),
            "w": float(normalized["w"]),
            "h": float(normalized["h"]),
        }

    def probe_capabilities(self, *, probe_awb_freeze: bool = False) -> CameraCapabilities:
        """Discover firmware features without disturbing production state by default."""
        caps = CameraCapabilities(probed=True)
        try:
            profile = self.get_profile()
            white_balance = profile.get("whiteBalance") or {}
            caps.awb_truthful_fields = (
                "awbStable" in white_balance or "awbFrames" in white_balance
            )
        except ImageControlAPIError:
            pass

        try:
            self.get_image_stats()
            caps.image_stats = True
        except ImageControlAPIError as error:
            # 404/501 = missing. Busy/network means the route is likely present.
            caps.image_stats = error.status not in {404, 501} and error.code not in {
                "unsupported_sensor",
                "http_error",
            }

        try:
            self.get_image_stats_roi()
            caps.image_stats_roi = True
        except ImageControlAPIError as error:
            caps.image_stats_roi = error.status not in {404, 501} and error.code not in {
                "unsupported_sensor",
                "http_error",
            }

        if probe_awb_freeze:
            try:
                self.freeze_awb()
                caps.awb_freeze = True
            except ImageControlAPIError as error:
                caps.awb_freeze = error.status not in {404, 501}
        else:
            # Infer from sibling firmware surface: truthful fields + image-stats
            # shipped together in the integration report.
            caps.awb_freeze = caps.awb_truthful_fields

        try:
            self._request("get", "/raw-stats", require_ok=True)
            caps.raw_stats = True
        except ImageControlAPIError as error:
            caps.raw_stats = False if error.status in {404, 501} else False

        self.capabilities = caps
        return caps

    @staticmethod
    def extract_manual_rgb(profile: dict[str, Any]) -> dict[str, int]:
        white_balance = profile.get("whiteBalance") or {}
        return {
            "red": int(white_balance["red"]),
            "green": int(white_balance["green"]),
            "blue": int(white_balance["blue"]),
        }

    @staticmethod
    def _parse_image_stats(payload: dict[str, Any]) -> ImageStats:
        global_block = payload.get("global") or {}
        roi_block = payload.get("roi")
        roi: ImageStatsRoi | None = None
        if isinstance(roi_block, dict):
            normalized = roi_block.get("normalized") or {}
            median_rg = roi_block.get("medianRg")
            median_bg = roi_block.get("medianBg")
            roi = ImageStatsRoi(
                x=float(normalized.get("x", DEFAULT_STATS_ROI["x"])),
                y=float(normalized.get("y", DEFAULT_STATS_ROI["y"])),
                w=float(normalized.get("w", DEFAULT_STATS_ROI["w"])),
                h=float(normalized.get("h", DEFAULT_STATS_ROI["h"])),
                samples=int(roi_block.get("samples") or 0),
                mean_r=float(roi_block.get("meanR") or 0.0),
                mean_g=float(roi_block.get("meanG") or 0.0),
                mean_b=float(roi_block.get("meanB") or 0.0),
                median_rg=float(median_rg) if median_rg is not None else None,
                median_bg=float(median_bg) if median_bg is not None else None,
                usable=bool(roi_block.get("usable", False)),
            )
        mean_y = global_block.get("meanY")
        return ImageStats(
            ok=bool(payload.get("ok")),
            sensor=str(payload["sensor"]) if payload.get("sensor") is not None else None,
            timestamp_ms=(
                int(payload["timestampMs"])
                if payload.get("timestampMs") is not None
                else None
            ),
            domain=str(payload["domain"]) if payload.get("domain") is not None else None,
            mean_y=float(mean_y) if mean_y is not None else None,
            clip_black_frac=(
                float(global_block["clipBlackFrac"])
                if global_block.get("clipBlackFrac") is not None
                else None
            ),
            clip_white_frac=(
                float(global_block["clipWhiteFrac"])
                if global_block.get("clipWhiteFrac") is not None
                else None
            ),
            white_balance=dict(payload.get("whiteBalance") or {}),
            roi=roi,
            raw=payload,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
        require_ok: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        request_timeout = self.timeout if timeout is None else timeout
        for attempt in range(5):
            try:
                call = getattr(self.session, method)
                kwargs: dict[str, Any] = {"timeout": request_timeout}
                if json_body is not None:
                    kwargs["json"] = json_body
                response = call(url, **kwargs)
            except requests.RequestException as exc:
                if attempt < 4:
                    self._sleep(min(4.0, 0.5 * (2**attempt)))
                    continue
                raise ImageControlAPIError(None, "network_error", str(exc)) from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise ImageControlAPIError(
                    response.status_code, "invalid_response", "response is not valid JSON"
                ) from exc

            if response.status_code == 200:
                if require_ok and (
                    not isinstance(payload, dict) or payload.get("ok") is not True
                ):
                    raise ImageControlAPIError(
                        response.status_code,
                        "invalid_response",
                        "successful response does not contain ok:true",
                    )
                if not isinstance(payload, dict):
                    raise ImageControlAPIError(
                        response.status_code,
                        "invalid_response",
                        "successful response is not a JSON object",
                    )
                return payload

            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = str(error.get("code") or "http_error")
            message = str(error.get("message") or response.reason or "request failed")
            field = error.get("field")
            if response.status_code == 503 and code == "camera_busy" and attempt < 4:
                self._sleep(min(4.0, 0.5 * (2**attempt)))
                continue
            raise ImageControlAPIError(
                response.status_code,
                code,
                message,
                str(field) if field is not None else None,
            )

        raise AssertionError("unreachable")
