"""Camera pipeline orchestrator.

This module manages the lifecycle of `camera_pipeline.py` and keeps storage usage
under control by purging the recordings directory when its backing volume
exceeds a configurable threshold. The storage watchdog runs in a background
thread so video capture remains non-blocking.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

LOG_FORMAT = "[%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

BASE_DIR = Path(__file__).resolve().parent
CAMERA_SCRIPT = BASE_DIR / "camera_pipeline.py"
RECORDINGS_DIR = Path("/media/aneesh/SSD/recordings/esp_cam1")
DISK_USAGE_THRESHOLD = 90  # percent
CHECK_INTERVAL_SECONDS = 5 * 60
STOP_TIMEOUT_SECONDS = 5.0

_camera_process_lock = threading.Lock()
_camera_process: Optional[subprocess.Popen] = None
_storage_monitor_lock = threading.Lock()
_storage_monitor: Optional["StorageMonitor"] = None
_shutdown_event = threading.Event()


def _resolve_python_command() -> str:
    """Return a Python executable suitable for spawning the pipeline."""
    candidates = [
        os.environ.get("PYTHON"),
        sys.executable,
        "python3",
        "python",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            subprocess.run([str(candidate), "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return str(candidate)
        except Exception:
            continue
    raise RuntimeError("Unable to locate a usable Python interpreter. Set the PYTHON environment variable.")


def _is_camera_running() -> bool:
    with _camera_process_lock:
        return _camera_process is not None and _camera_process.poll() is None


def start_camera_pipeline() -> Optional[subprocess.Popen]:
    """Launch the camera pipeline if it is not already running."""
    global _camera_process

    with _camera_process_lock:
        if _camera_process and _camera_process.poll() is None:
            logging.info("[orchestrator] Camera pipeline already running (pid=%s).", _camera_process.pid)
            return _camera_process

        if not CAMERA_SCRIPT.exists():
            raise FileNotFoundError(f"Camera script not found at {CAMERA_SCRIPT}")

        python_cmd = _resolve_python_command()
        logging.info("[orchestrator] Starting camera pipeline with %s %s", python_cmd, CAMERA_SCRIPT)
        try:
            proc = subprocess.Popen([python_cmd, str(CAMERA_SCRIPT)], stdin=None, stdout=None, stderr=None)
        except Exception as exc:
            logging.exception("[orchestrator] Failed to start camera pipeline")
            raise RuntimeError("Unable to start camera pipeline") from exc

        _camera_process = proc
        logging.info("[orchestrator] Camera pipeline started (pid=%s).", proc.pid)
        return proc


def stop_camera_pipeline(sig: signal.Signals = signal.SIGTERM) -> None:
    """Terminate the camera pipeline process if it is running."""
    global _camera_process

    with _camera_process_lock:
        proc = _camera_process
        _camera_process = None

    if proc is None:
        return

    if proc.poll() is not None:
        return

    logging.info("[orchestrator] Stopping camera pipeline with %s.", sig.name)
    try:
        proc.send_signal(sig)
    except Exception:
        logging.exception("[orchestrator] Failed to signal camera pipeline")

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)

    if proc.poll() is None:
        logging.warning("[orchestrator] Pipeline did not exit in %.1fs, sending SIGKILL.", STOP_TIMEOUT_SECONDS)
        try:
            proc.kill()
        except Exception:
            logging.exception("[orchestrator] Failed to SIGKILL pipeline")
        else:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logging.warning("[orchestrator] Pipeline did not terminate after SIGKILL.")


def restart_camera_pipeline() -> Optional[subprocess.Popen]:
    stop_camera_pipeline()
    return start_camera_pipeline()


def _delete_directory_contents(directory: Path) -> None:
    """Remove all files and subdirectories inside the given directory."""
    if not directory.exists():
        return

    for entry in directory.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except FileNotFoundError:
            continue
        except Exception:
            logging.exception("[cleanup] Failed to remove %s", entry)
            raise


def _get_usage_percent(path: Path) -> int:
    usage = shutil.disk_usage(path)
    percent = int((usage.used / usage.total) * 100)
    return percent


def check_storage_and_cleanup() -> None:
    """Ensure recording storage is below threshold; purge if necessary."""
    if not RECORDINGS_DIR.exists():
        logging.warning("[cleanup] Recordings directory %s not found; skipping.", RECORDINGS_DIR)
        return

    try:
        usage_percent = _get_usage_percent(RECORDINGS_DIR)
    except Exception:
        logging.exception("[cleanup] Unable to calculate disk usage for %s", RECORDINGS_DIR)
        return

    # Always log disk usage so you can see it's working
    logging.info("[cleanup] Disk usage check: %s%% (threshold: %s%%)", usage_percent, DISK_USAGE_THRESHOLD)

    if usage_percent < DISK_USAGE_THRESHOLD:
        return

    logging.warning(
        "[cleanup] Disk usage %s%% exceeds threshold %s%%. Purging recordings directory.",
        usage_percent,
        DISK_USAGE_THRESHOLD,
    )

    was_running = _is_camera_running()
    stop_camera_pipeline()

    try:
        _delete_directory_contents(RECORDINGS_DIR)
    except Exception:
        logging.exception("[cleanup] Failed to purge recordings directory")
    else:
        logging.info("[cleanup] Recordings directory purged successfully.")

    if was_running:
        start_camera_pipeline()


class StorageMonitor(threading.Thread):
    """Background thread that periodically triggers storage cleanup."""

    def __init__(self, interval_seconds: int) -> None:
        super().__init__(daemon=True)
        self.interval = interval_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logging.info("[monitor] Storage monitor thread started (interval=%ss).", self.interval)
        while not self._stop_event.is_set():
            try:
                check_storage_and_cleanup()
            except Exception:
                logging.exception("[monitor] Storage cleanup failed")
            if self._stop_event.wait(self.interval):
                break
        logging.info("[monitor] Storage monitor thread stopped.")


def start_orchestrator() -> None:
    """Start the camera pipeline and the storage monitor."""
    global _storage_monitor

    start_camera_pipeline()

    with _storage_monitor_lock:
        if _storage_monitor is None or not _storage_monitor.is_alive():
            _storage_monitor = StorageMonitor(CHECK_INTERVAL_SECONDS)
            _storage_monitor.start()
            logging.info("[orchestrator] Storage cleanup job scheduled every %s seconds.", CHECK_INTERVAL_SECONDS)


def shutdown_orchestrator(sig: signal.Signals = signal.SIGTERM) -> None:
    """Stop the storage monitor and camera pipeline."""
    global _storage_monitor

    logging.info("[orchestrator] Shutting down (signal=%s).", sig.name)

    _shutdown_event.set()

    with _storage_monitor_lock:
        monitor = _storage_monitor
        _storage_monitor = None

    if monitor is not None:
        monitor.stop()
        monitor.join(timeout=STOP_TIMEOUT_SECONDS)

    stop_camera_pipeline(sig)


def _handle_signal(signum: int, _frame) -> None:
    logging.info("[orchestrator] Received signal %s.", signal.Signals(signum).name)
    shutdown_orchestrator(signal.Signals(signum))
    _shutdown_event.set()


def main() -> None:
    start_orchestrator()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGQUIT", signal.SIGTERM)):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            continue

    logging.info("[orchestrator] Running. Press Ctrl+C to exit.")
    try:
        while not _shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _handle_signal(signal.SIGINT, None)


if __name__ == "__main__":
    main()
