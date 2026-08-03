from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal


DEFAULT_COLOR_PROFILE_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "camera_color_profile.json"
)
DEFAULT_NIGHT_COLOR_PROFILE_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "camera_color_profile_night.json"
)
DEFAULT_LUMA_OFFSET = 20
DEFAULT_CONTRAST_REGISTERS = (48, 48, 48, 10)
DEFAULT_NIGHT_START_HOUR = 19
DEFAULT_NIGHT_END_HOUR = 6

ColorPeriod = Literal["day", "night"]


@dataclass(frozen=True)
class CameraColorProfile:
    red: int
    green: int
    blue: int
    saturation_u: int
    saturation_v: int
    luma_offset: int = DEFAULT_LUMA_OFFSET
    contrast_registers: tuple[int, int, int, int] = DEFAULT_CONTRAST_REGISTERS
    period: ColorPeriod = "day"

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        period: ColorPeriod = "day",
    ) -> "CameraColorProfile":
        white_balance = data["whiteBalance"]
        saturation = data["color"]["saturation"]
        tone = data.get("tone") or {}
        contrast = tone.get("contrastRegisters", list(DEFAULT_CONTRAST_REGISTERS))
        if not isinstance(contrast, list) or len(contrast) != 4:
            raise ValueError("tone.contrastRegisters must be a list of 4 integers")
        profile = cls(
            red=int(white_balance["red"]),
            green=int(white_balance["green"]),
            blue=int(white_balance["blue"]),
            saturation_u=int(saturation["u"]),
            saturation_v=int(saturation["v"]),
            luma_offset=int(tone.get("lumaOffset", DEFAULT_LUMA_OFFSET)),
            contrast_registers=tuple(int(value) for value in contrast),
            period=period,
        )
        for name in ("red", "green", "blue", "saturation_u", "saturation_v"):
            value = getattr(profile, name)
            if not 0 <= value <= 255:
                raise ValueError(f"{name} must be between 0 and 255")
        if not -32 <= profile.luma_offset <= 32:
            raise ValueError("luma_offset must be between -32 and 32")
        if any(not 0 <= value <= 255 for value in profile.contrast_registers):
            raise ValueError("contrast register values must be between 0 and 255")
        return profile

    @classmethod
    def load(cls, path: Path | None = None) -> "CameraColorProfile":
        """Load the day color profile (or an explicit path override)."""
        configured = os.getenv("CCTV_COLOR_PROFILE_PATH")
        selected = (
            Path(configured).expanduser()
            if configured and path is None
            else (path or DEFAULT_COLOR_PROFILE_PATH)
        )
        data: Any = json.loads(selected.read_text(encoding="utf-8"))
        return cls.from_dict(data, period="day")

    @classmethod
    def load_night(cls, path: Path | None = None) -> "CameraColorProfile":
        configured = os.getenv("CCTV_COLOR_PROFILE_NIGHT_PATH")
        selected = (
            Path(configured).expanduser()
            if configured and path is None
            else (path or DEFAULT_NIGHT_COLOR_PROFILE_PATH)
        )
        data: Any = json.loads(selected.read_text(encoding="utf-8"))
        return cls.from_dict(data, period="night")

    @classmethod
    def load_active(
        cls,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        day_path: Path | None = None,
        night_path: Path | None = None,
    ) -> "CameraColorProfile":
        """Load day or night profile based on local wall-clock hours."""
        period = color_period_for_now(wall_clock=wall_clock)
        if period == "night":
            try:
                return cls.load_night(night_path)
            except FileNotFoundError:
                # Night file is optional; fall back to day until one is supplied.
                profile = cls.load(day_path)
                return CameraColorProfile(
                    red=profile.red,
                    green=profile.green,
                    blue=profile.blue,
                    saturation_u=profile.saturation_u,
                    saturation_v=profile.saturation_v,
                    luma_offset=profile.luma_offset,
                    contrast_registers=profile.contrast_registers,
                    period="night",
                )
        return cls.load(day_path)

    def white_balance_patch(self) -> dict[str, Any]:
        return {
            "whiteBalance": {
                "auto": False,
                "red": self.red,
                "green": self.green,
                "blue": self.blue,
            }
        }

    def saturation_patch(self) -> dict[str, Any]:
        return {"color": {"saturation": {"u": self.saturation_u, "v": self.saturation_v}}}

    def tone_patch(self) -> dict[str, Any]:
        return {
            "tone": {
                "lumaOffset": self.luma_offset,
                "contrastRegisters": list(self.contrast_registers),
            }
        }


def color_period_for_now(
    *,
    wall_clock: Callable[[], datetime] | None = None,
    night_start_hour: int | None = None,
    night_end_hour: int | None = None,
) -> ColorPeriod:
    """Return ``night`` between start (inclusive) and end (exclusive), wrapping midnight."""
    start = (
        night_start_hour
        if night_start_hour is not None
        else int(os.getenv("CCTV_COLOR_NIGHT_START_HOUR", str(DEFAULT_NIGHT_START_HOUR)))
    )
    end = (
        night_end_hour
        if night_end_hour is not None
        else int(os.getenv("CCTV_COLOR_NIGHT_END_HOUR", str(DEFAULT_NIGHT_END_HOUR)))
    )
    if not 0 <= start <= 23 or not 0 <= end <= 23 or start == end:
        raise ValueError("color night hours must be distinct values from 0 to 23")
    clock = wall_clock or datetime.now
    hour = clock().hour
    if start < end:
        is_night = start <= hour < end
    else:
        is_night = hour >= start or hour < end
    return "night" if is_night else "day"
