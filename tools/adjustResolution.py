import requests
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CAMERA_IP = "192.168.0.13"


def adjustResolution(val: int) -> str:

    if not isinstance(val, int):
        logger.error(f"Invalid resolution value: {val}. Must be an integer.")
        return "Failure"

    try:
        url = f"http://{CAMERA_IP}/control?var=framesize&val={val}"
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            logger.info(f"Successfully set resolution to {val}")
            return "Success"
        else:
            logger.warning(
                f"Failed to set resolution. Status code: {response.status_code}"
            )
            return "Failure"

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error while adjusting resolution: {e}")
        return "Failure"
    except Exception as e:
        logger.error(f"Unexpected error while adjusting resolution: {e}")
        return "Failure"
