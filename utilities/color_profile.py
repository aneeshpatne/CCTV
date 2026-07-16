from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_COLOR_PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "camera_color_profile.json"


@dataclass(frozen=True)
class CameraColorProfile:
    red: int
    green: int
    blue: int
    saturation_u: int
    saturation_v: int

    @classmethod
    def load(cls, path: Path | None = None) -> "CameraColorProfile":
        configured = os.getenv("CCTV_COLOR_PROFILE_PATH")
        selected = Path(configured).expanduser() if configured else (path or DEFAULT_COLOR_PROFILE_PATH)
        data: Any = json.loads(selected.read_text(encoding="utf-8"))
        white_balance = data["whiteBalance"]
        saturation = data["color"]["saturation"]
        profile = cls(
            red=int(white_balance["red"]),
            green=int(white_balance["green"]),
            blue=int(white_balance["blue"]),
            saturation_u=int(saturation["u"]),
            saturation_v=int(saturation["v"]),
        )
        for name, value in profile.__dict__.items():
            if not 0 <= value <= 255:
                raise ValueError(f"{name} must be between 0 and 255")
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
