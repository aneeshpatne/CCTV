from __future__ import annotations

import logging
import os
import socket
import time
from requests.exceptions import RequestException

from tools.changeQuality import change_quality
from tools.changeClock import change_clock
from utilities.esp32cam_client import (
    CameraStatus,
    get_camera_status,
    get_camera_status_with_retry,
)

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
count = 1

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
ESP32CAM_RECOVERY_REDIS_KEY = "esp32cam:recovery"
RECOVERY_TRIGGER_FAILURES = 3
RECOVERY_STATUS_DELAY_SECONDS = 5


class CameraRecoveryMode(RuntimeError):
    def __init__(self, status: CameraStatus):
        self.status = status
        detail = "Device is in recovery / OTA-only mode"
        if status.reason:
            detail = f"{detail}: {status.reason}"
        if status.bad_boot_count is not None:
            detail = f"{detail} (badBootCount={status.bad_boot_count})"
        super().__init__(detail)


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
    status = get_camera_status(timeout=2)
    if status.source != "status-v2":
        return None
    return status.raw


def run_recovery_mode() -> bool:
    logger.warning("Entering ESP32-CAM recovery mode")
    try:
        redis_set(ESP32CAM_RECOVERY_REDIS_KEY, "true")
    except Exception as err:
        logger.warning(f"Failed to enable recovery flag in Redis: {err}")
        return False

    time.sleep(RECOVERY_STATUS_DELAY_SECONDS)
    status = get_camera_status(timeout=2)
    logger.info(f"Recovery status response: {status.raw}")

    if status.ota_only:
        try:
            redis_set(ESP32CAM_RECOVERY_REDIS_KEY, "false")
        except Exception as err:
            logger.warning(f"Failed to disable recovery flag in Redis: {err}")
            return False

        logger.warning(
            "Device is in recovery / OTA-only mode. Leaving reset as explicit user action."
        )
        return True

    logger.warning("Recovery mode did not reach OTA")
    return False


def startup(target_framesize: int = 12):
    global count
    if target_framesize not in {11, 12}:
        raise ValueError(f"Unsupported target framesize: {target_framesize}")

    consecutive_connection_failures = 0
    while True:
        camera_status = get_camera_status_with_retry(attempts=3, timeout=2)
        if camera_status.ota_only:
            logger.warning("%s", CameraRecoveryMode(camera_status))
            raise CameraRecoveryMode(camera_status)

        if not camera_status.camera_online:
            consecutive_connection_failures += 1
            logger.warning(
                f"Camera offline or starting, retrying, attempt number: {count}"
            )
            count += 1
            if consecutive_connection_failures >= RECOVERY_TRIGGER_FAILURES:
                if run_recovery_mode():
                    recovery_status = get_camera_status(timeout=2)
                    if recovery_status.ota_only:
                        raise CameraRecoveryMode(recovery_status)
                consecutive_connection_failures = 0
            time.sleep(10)
            continue
        consecutive_connection_failures = 0
        stat = camera_status.framesize
        logger.info("Camera Initiated")
        logger.info(f"Initial status: {camera_status.raw}")
        if stat is None:
            logger.warning("Camera status missing framesize; retrying startup")
            time.sleep(5)
            continue
        current_framesize = int(stat)
        if current_framesize > target_framesize:
            resolution_steps = [target_framesize]
        else:
            resolution_steps = list(
                range(max(current_framesize, 10) + 1, target_framesize + 1)
            )

        resolution_set = True
        for next_framesize in resolution_steps:
            logger.info(f"Current Resolution: {current_framesize}")
            logger.info(f"Attempting to set Current Resolution to: {next_framesize}")

            # Wrap change_quality in try-except to handle connection timeouts
            try:
                change_quality(next_framesize)
                time.sleep(3)
            except RequestException as err:
                logger.warning(f"Failed to change quality (connection error): {err}")
                # Camera likely crashed - restart from beginning
                time.sleep(5)
                resolution_set = False
                break
            except Exception as err:
                logger.warning(f"Unexpected error changing quality: {err}")
                time.sleep(5)
                resolution_set = False
                break

            camera_status = get_camera_status_with_retry(attempts=3, timeout=2)
            if camera_status.ota_only:
                logger.warning("%s", CameraRecoveryMode(camera_status))
                raise CameraRecoveryMode(camera_status)

            stat = camera_status.framesize if camera_status.camera_online else None
            if stat is None or int(stat) != next_framesize:
                logger.warning("Resolution Change Failed")
                time.sleep(5)
                resolution_set = False
                break
            current_framesize = next_framesize

        if resolution_set and current_framesize == target_framesize:
            time.sleep(6)
            logger.info(f"Resolution Set Successfully to {target_framesize}")
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
