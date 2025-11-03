import requests
from requests.exceptions import RequestException
import logging

logger = logging.getLogger(__name__)

def reset():
    """Reset the camera. Returns True on success, False on failure."""
    try:
        res = requests.get("http://192.168.1.119/reset", timeout=2)
        res.raise_for_status()
        logger.info("Camera reset successful")
        return True
    except RequestException as err:
        logger.warning(f"Camera reset failed: {err}")
        return False
    except Exception as err:
        logger.error(f"Unexpected error during camera reset: {err}")
        return False
    