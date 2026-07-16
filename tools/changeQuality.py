import os

import requests
from requests.exceptions import RequestException


def change_quality(quality):
    """Change camera quality/resolution setting.

    Args:
        quality: Resolution value to set

    Raises:
        RequestException: If the request fails (timeout, connection error, etc.)
    """
    camera_base_url = os.getenv("ESP32CAM_BASE_URL", "http://192.168.0.13").rstrip("/")
    res = requests.get(
        f"{camera_base_url}/control", params={"var": "framesize", "val": quality}, timeout=2
    )
    res.raise_for_status()  # Raise exception for bad status codes
    return res
