import requests
from requests.exceptions import RequestException
import logging

from utilities.esp32cam_client import CAMERA_BASE_URL
from utilities.startup import ESP32CAM_RECOVERY_REDIS_KEY, redis_set

logger = logging.getLogger(__name__)


def reset():
    """Exit recovery intent in Redis, then reset the camera."""
    try:
        redis_set(ESP32CAM_RECOVERY_REDIS_KEY, "false")
        logger.info("ESP32-CAM recovery flag cleared before reset")
    except Exception as err:
        logger.warning(f"Failed to clear ESP32-CAM recovery flag before reset: {err}")
        return False

    try:
        res = requests.get(f"{CAMERA_BASE_URL}/reset", timeout=2)
        res.raise_for_status()
        logger.info("Camera reset successful")
        return True
    except RequestException as err:
        logger.warning(f"Camera reset failed: {err}")
        return False
    except Exception as err:
        logger.error(f"Unexpected error during camera reset: {err}")
        return False


if __name__ == "__main__":
    raise SystemExit(0 if reset() else 1)
