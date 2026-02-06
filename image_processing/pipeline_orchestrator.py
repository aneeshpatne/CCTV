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
REPO_ROOT = BASE_DIR.parent
RECORDINGS_DIR = Path(
    os.getenv("CCTV_RECORDINGS_DIR", "/Volumes/drive/CCTV/recordings/esp_cam1")
).expanduser()
if not RECORDINGS_DIR.exists():
    RECORDINGS_DIR = REPO_ROOT / "recordings" / "esp_cam1"
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

        python_cmd = _resolve_python_command()
        logging.info("[orchestrator] Starting camera pipeline with %s -m image_processing.camera_pipeline", python_cmd)
        try:
            proc = subprocess.Popen(
                [python_cmd, "-m", "image_processing.camera_pipeline"],
                cwd=str(REPO_ROOT),
                stdin=None, stdout=None, stderr=None,
            )
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


def _get_usage_percent(path: Path) -> int:
    """Get disk usage percentage for the filesystem containing the given path."""
    usage = shutil.disk_usage(path)
    percent = int((usage.used / usage.total) * 100)
    return percent


def _delete_oldest_files_until_threshold(directory: Path, target_percent: int) -> None:
    """Delete oldest files in directory until disk usage drops below target_percent."""
    if not directory.exists():
        logging.warning("[cleanup] Directory %s does not exist", directory)
        return

    # Get all video files sorted by modification time (oldest first)
    files = []
    for entry in directory.rglob("*.mp4"):
        if entry.is_file():
            try:
                mtime = entry.stat().st_mtime
                size = entry.stat().st_size
                files.append((mtime, size, entry))
            except (FileNotFoundError, PermissionError):
                continue

    if not files:
        logging.warning("[cleanup] No video files found to delete in %s", directory)
        return

    # Sort by modification time (oldest first)
    files.sort(key=lambda x: x[0])

    deleted_count = 0
    deleted_size = 0

    for mtime, size, file_path in files:
        current_usage = _get_usage_percent(directory)
        
        if current_usage < target_percent:
            logging.info(
                "[cleanup] Target usage reached: %s%% < %s%%. Deleted %d files (%d MB).",
                current_usage,
                target_percent,
                deleted_count,
                deleted_size // (1024 * 1024),
            )
            return

        try:
            file_path.unlink()
            deleted_count += 1
            deleted_size += size
            logging.info("[cleanup] Deleted old file: %s (%d MB)", file_path.name, size // (1024 * 1024))
        except FileNotFoundError:
            continue
        except Exception:
            logging.exception("[cleanup] Failed to delete %s", file_path)
            continue

    # Final check after deleting all files
    final_usage = _get_usage_percent(directory)
    logging.info(
        "[cleanup] Cleanup complete. Deleted %d files (%d MB). Final usage: %s%%",
        deleted_count,
        deleted_size // (1024 * 1024),
        final_usage,
    )


def check_storage_and_cleanup() -> None:
    """Check storage and delete old files if needed (runs in background thread)."""
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
        "[cleanup] Disk usage %s%% exceeds threshold %s%%. Starting cleanup (camera keeps running).",
        usage_percent,
        DISK_USAGE_THRESHOLD,
    )

    try:
        # Delete old files without stopping camera - already running in background thread
        _delete_oldest_files_until_threshold(RECORDINGS_DIR, DISK_USAGE_THRESHOLD)
    except Exception:
        logging.exception("[cleanup] Failed during cleanup operation")


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
