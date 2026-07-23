from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_COLOR_PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "camera_color_profile.json"
DEFAULT_LUMA_OFFSET = 20
DEFAULT_CONTRAST_REGISTERS = (48, 48, 48, 10)


@dataclass(frozen=True)
class CameraColorProfile:
    red: int
    green: int
    blue: int
    saturation_u: int
    saturation_v: int
    luma_offset: int = DEFAULT_LUMA_OFFSET
    contrast_registers: tuple[int, int, int, int] = DEFAULT_CONTRAST_REGISTERS

    @classmethod
    def load(cls, path: Path | None = None) -> "CameraColorProfile":
        configured = os.getenv("CCTV_COLOR_PROFILE_PATH")
        selected = Path(configured).expanduser() if configured else (path or DEFAULT_COLOR_PROFILE_PATH)
        data: Any = json.loads(selected.read_text(encoding="utf-8"))
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
