import logging
import os
import socket
import time
import requests
from requests.exceptions import RequestException

from tools.status import status
from tools.changeQuality import change_quality
from tools.reset import reset
from tools.changeClock import change_clock

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
count = 1

CAMERA_BASE_URL = os.getenv("ESP32CAM_BASE_URL", "http://192.168.0.13")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
ESP32CAM_RECOVERY_REDIS_KEY = "esp32cam:recovery"
RECOVERY_TRIGGER_FAILURES = 3
RECOVERY_STATUS_DELAY_SECONDS = 5


def redis_set(key: str, value: str) -> None:
    def encode_command(*command_parts: str) -> bytes:
        payload = f"*{len(command_parts)}\r\n"
        for part in command_parts:
            encoded = part.encode("utf-8")
            payload += f"${len(encoded)}\r\n{part}\r\n"
        return payload.encode("utf-8")

    def expect_ok(stream) -> None:
        response = stream.readline()
        if response.startswith(b"-"):
            raise RuntimeError(response[1:].decode("utf-8", errors="replace").strip())
        if response != b"+OK\r\n":
            raise RuntimeError(f"Unexpected Redis response: {response!r}")

    with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=2.0) as client:
        stream = client.makefile("rb")

        if REDIS_PASSWORD:
            client.sendall(encode_command("AUTH", REDIS_PASSWORD))
            expect_ok(stream)

        if REDIS_DB:
            client.sendall(encode_command("SELECT", str(REDIS_DB)))
            expect_ok(stream)

        client.sendall(encode_command("SET", key, value))
        expect_ok(stream)


def status_v2() -> dict | None:
    try:
        res = requests.get(f"{CAMERA_BASE_URL}/status-v2", timeout=2)
        res.raise_for_status()
        data = res.json()
    except (RequestException, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    return data


def run_recovery_mode() -> bool:
    logger.warning("Entering ESP32-CAM recovery mode")
    try:
        redis_set(ESP32CAM_RECOVERY_REDIS_KEY, "true")
    except Exception as err:
        logger.warning(f"Failed to enable recovery flag in Redis: {err}")
        return False

    time.sleep(RECOVERY_STATUS_DELAY_SECONDS)
    data = status_v2()
    logger.info(f"Recovery status-v2 response: {data}")

    if data and data.get("mode") == "OTA":
        try:
            redis_set(ESP32CAM_RECOVERY_REDIS_KEY, "false")
        except Exception as err:
            logger.warning(f"Failed to disable recovery flag in Redis: {err}")
            return False

        logger.info("Recovery mode reached OTA; resetting camera")
        reset()
        return True

    logger.warning("Recovery mode did not reach OTA")
    return False


def startup():
    global count
    consecutive_connection_failures = 0
    while True:
        stat = status()
        if stat == None:
            consecutive_connection_failures += 1
            logger.warning(
                f"Camera Connection Failed Retrying, Attempt Number: {count}"
            )
            count += 1
            if consecutive_connection_failures >= RECOVERY_TRIGGER_FAILURES:
                run_recovery_mode()
                consecutive_connection_failures = 0
            # reset()
            time.sleep(10)
            continue
        consecutive_connection_failures = 0
        i = 1
        logger.info("Camera Initiated")
        logger.info(f"Initial Quality: {stat}")
        i = max(int(stat), 10)
        while i < 12:
            logger.info(f"Current Resolution: {i}")
            logger.info(f"Attempting to set Current Resolution to: {i + 1}")

            # Wrap change_quality in try-except to handle connection timeouts
            try:
                change_quality(i + 1)
                time.sleep(3)
            except RequestException as err:
                logger.warning(f"Failed to change quality (connection error): {err}")
                # Camera likely crashed - restart from beginning
                time.sleep(5)
                break
            except Exception as err:
                logger.warning(f"Unexpected error changing quality: {err}")
                time.sleep(5)
                break

            stat = status()
            if stat == None or int(stat) != i + 1:
                logger.warning("Resolution Change Failed")
                i = 10
                time.sleep(5)
                continue
            i += 1

        # Only log success if we actually completed the loop
        if i >= 12:
            time.sleep(6)
            logger.info(f"Resolution Set Successfully to {i}")
        else:
            logger.warning("Resolution setting incomplete - will retry")
            continue

        # Set camera clock
        try:
            logger.info("Setting camera clock to 20")
            change_clock(20)
        except RequestException as err:
            logger.warning(f"Setting camera clock failed: {err}")

        time.sleep(2)
        logger.info("Camera startup sequence completed")
        break


if __name__ == "__main__":
    startup()
