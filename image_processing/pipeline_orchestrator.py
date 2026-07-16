"""Camera pipeline orchestrator.

This module manages the lifecycle of `camera_pipeline.py` and keeps storage usage
under control by purging the recordings directory when its backing volume
exceeds a configurable threshold. The storage watchdog runs in a background
thread so video capture remains non-blocking.
"""

from __future__ import annotations

import logging
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import requests

from utilities.motion_db_new import annotate_motion_event, log_motion_event
from utilities.recording_catalog import RecordingCatalog
from utilities.dynamic_resolution import DynamicResolutionController, ResolutionDecision
from utilities.esp32cam_client import get_camera_status_with_retry
from utilities.startup import CameraRecoveryMode, startup

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
DISK_USAGE_TARGET = 85  # cleanup hysteresis target
CHECK_INTERVAL_SECONDS = 5 * 60
STOP_TIMEOUT_SECONDS = 5.0
NATIVE_FAILURE_LIMIT = 3
NATIVE_FAILURE_WINDOW_SECONDS = 5 * 60
POST_CONNECT_ADJUSTMENT_DELAY_SECONDS = float(
    os.getenv("CCTV_POST_CONNECT_ADJUSTMENT_DELAY_SECONDS", "20")
)
CAMERA_BASE_URL = os.getenv("ESP32CAM_BASE_URL", "http://192.168.0.13").rstrip("/")
IST = ZoneInfo("Asia/Kolkata")

_camera_process_lock = threading.Lock()
_camera_process: Optional[subprocess.Popen] = None
_camera_monitor_lock = threading.Lock()
_camera_monitor: Optional["CameraProcessMonitor"] = None
_storage_monitor_lock = threading.Lock()
_storage_monitor: Optional["StorageMonitor"] = None
_shutdown_event = threading.Event()
_event_reader: Optional["NativeEventReader"] = None
_event_reader_lock = threading.Lock()
_native_failures: deque[float] = deque()
_native_fallback_latched = False
_active_backend = "python"
_recording_catalog = RecordingCatalog(RECORDINGS_DIR)
_camera_recovery_lock = threading.Lock()
_camera_recovery_thread: Optional[threading.Thread] = None
_camera_adjustment_lock = threading.Lock()
_camera_adjustment_generation = 0
_camera_configuration_lock = threading.Lock()
_resolution_change_lock = threading.Lock()
_resolution_change_thread: Optional[threading.Thread] = None
_resolution_controller = DynamicResolutionController.from_environment(initial_framesize=12)


def _cancel_post_connect_adjustments() -> None:
    """Invalidate delayed adjustments belonging to an obsolete connection."""
    global _camera_adjustment_generation
    with _camera_adjustment_lock:
        _camera_adjustment_generation += 1


def _adjustment_is_current(generation: int) -> bool:
    with _camera_adjustment_lock:
        return not _shutdown_event.is_set() and generation == _camera_adjustment_generation


def _wait_for_adjustment(generation: int, seconds: float) -> bool:
    if _shutdown_event.wait(seconds):
        return False
    return _adjustment_is_current(generation)


def _set_camera_control(name: str, value: int) -> bool:
    try:
        response = requests.get(
            f"{CAMERA_BASE_URL}/control",
            params={"var": name, "val": value},
            timeout=2,
        )
        response.raise_for_status()
        logging.info("[adjustments] Set %s=%s.", name, value)
        return True
    except requests.RequestException:
        logging.exception("[adjustments] Failed to set %s=%s.", name, value)
        return False


def _schedule_post_connect_adjustments() -> None:
    """Apply the legacy AWB/exposure/AGC policy after MJPEG stabilizes."""
    global _camera_adjustment_generation

    with _camera_adjustment_lock:
        _camera_adjustment_generation += 1
        generation = _camera_adjustment_generation

    def adjust() -> None:
        logging.info(
            "[adjustments] Signal restored; waiting %.0fs before camera tuning.",
            POST_CONNECT_ADJUSTMENT_DELAY_SECONDS,
        )
        if not _wait_for_adjustment(generation, POST_CONNECT_ADJUSTMENT_DELAY_SECONDS):
            return

        _set_camera_control("awb", 0)
        if not _wait_for_adjustment(generation, 2):
            return

        current_hour_ist = datetime.now(IST).hour
        if 12 <= current_hour_ist < 18:
            logging.info("[adjustments] Skipping ae_level during 12pm-6pm IST window.")
        else:
            _set_camera_control("ae_level", 2)

        if not _wait_for_adjustment(generation, 2):
            return
        _set_camera_control("agc", 0)
        logging.info("[adjustments] Post-connect camera tuning complete.")

    threading.Thread(
        target=adjust,
        daemon=True,
        name="cctv-camera-adjustments",
    ).start()


def _schedule_camera_recovery(reason: str) -> None:
    """Run the full ESP startup loop once while native output shows no signal."""
    global _camera_recovery_thread

    with _camera_recovery_lock:
        if _shutdown_event.is_set():
            return
        if _camera_recovery_thread and _camera_recovery_thread.is_alive():
            logging.info("[recovery] Camera startup already running; coalescing disconnect (%s).", reason)
            return

        def recover() -> None:
            logging.warning("[recovery] MJPEG disconnected (%s); running full camera startup.", reason)
            try:
                with _camera_configuration_lock:
                    startup(target_framesize=_resolution_controller.selected_framesize)
            except CameraRecoveryMode:
                logging.exception("[recovery] Camera entered OTA/recovery mode during startup.")
            except Exception:
                logging.exception("[recovery] Camera startup failed; native reconnect will retry.")
            else:
                logging.info("[recovery] Full camera startup sequence completed after disconnect.")

        _camera_recovery_thread = threading.Thread(
            target=recover,
            daemon=True,
            name="cctv-camera-recovery",
        )
        _camera_recovery_thread.start()


def _schedule_resolution_change(decision: ResolutionDecision) -> None:
    """Apply one verified framesize change without blocking native event reads."""
    global _resolution_change_thread

    with _resolution_change_lock:
        if _shutdown_event.is_set():
            _resolution_controller.complete_change(decision.framesize, success=False)
            return
        if _resolution_change_thread and _resolution_change_thread.is_alive():
            return

        def adjust() -> None:
            target = decision.framesize
            logging.info(
                "[resolution] Sustained brightness %.1f%%; switching framesize to %d.",
                decision.average_brightness * 100,
                target,
            )
            success = False
            try:
                with _camera_configuration_lock:
                    if not _set_camera_control("framesize", target):
                        return
                    if _shutdown_event.wait(3):
                        return
                    status = get_camera_status_with_retry(attempts=3, timeout=2)
                    success = (
                        status.camera_online
                        and status.framesize is not None
                        and int(status.framesize) == target
                    )
            except Exception:
                logging.exception("[resolution] Failed while verifying framesize %d.", target)
            finally:
                _resolution_controller.complete_change(target, success=success)
                if success:
                    logging.info(
                        "[resolution] Framesize %d verified; 15-minute cooldown started.",
                        target,
                    )
                elif not _shutdown_event.is_set():
                    logging.warning(
                        "[resolution] Framesize %d was not verified; a later brightness sample may retry.",
                        target,
                    )

        _resolution_change_thread = threading.Thread(
            target=adjust,
            daemon=True,
            name="cctv-dynamic-resolution",
        )
        _resolution_change_thread.start()


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
            subprocess.run(
                [str(candidate), "--version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return str(candidate)
        except Exception:
            continue
    raise RuntimeError(
        "Unable to locate a usable Python interpreter. Set the PYTHON environment variable."
    )


def _is_camera_running() -> bool:
    with _camera_process_lock:
        return _camera_process is not None and _camera_process.poll() is None


def _resolve_native_binary() -> Path | None:
    configured = os.getenv("CCTV_NATIVE_BINARY")
    candidates = [
        Path(configured).expanduser() if configured else None,
        REPO_ROOT / "native" / ".build" / "release" / "cctv-capture",
        REPO_ROOT / "native" / ".build" / "arm64-apple-macosx" / "release" / "cctv-capture",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _requested_backend() -> str:
    requested = os.getenv("CCTV_PIPELINE_BACKEND", "native").strip().lower()
    if requested not in {"native", "python", "auto"}:
        logging.warning("[orchestrator] Unknown CCTV_PIPELINE_BACKEND=%s; using native.", requested)
        requested = "native"
    if requested == "python" or _native_fallback_latched:
        return "python"
    if _resolve_native_binary() is None:
        logging.warning("[orchestrator] Native binary unavailable; using Python fallback.")
        return "python"
    return "native"


class NativeEventReader(threading.Thread):
    """Consume the native worker's low-volume JSON event stream."""

    def __init__(self, read_fd: int):
        super().__init__(daemon=True, name="cctv-native-events")
        self.read_fd = read_fd

    def run(self) -> None:
        try:
            with os.fdopen(self.read_fd, "r", encoding="utf-8") as stream:
                for line in stream:
                    if _shutdown_event.is_set():
                        break
                    try:
                        event = json.loads(line)
                        self._handle(event)
                    except Exception:
                        logging.exception("[native-event] Invalid event: %s", line[:500])
        except OSError:
            if not _shutdown_event.is_set():
                logging.exception("[native-event] Event pipe failed")

    @staticmethod
    def _handle(event: dict) -> None:
        if event.get("version") != 1 or not isinstance(event.get("payload"), dict):
            raise ValueError("unsupported native event envelope")
        event_type = event.get("type")
        payload = event["payload"]
        if event_type == "motion.finalized":
            start = datetime.fromtimestamp(float(payload["start_time"]))
            end = datetime.fromtimestamp(float(payload["end_time"]))
            motion = log_motion_event(
                start_time=start,
                end_time=end,
                duration=float(payload.get("duration", (end - start).total_seconds())),
            )
            annotate_motion_event(
                motion.id,
                detector_version=str(payload.get("detector_version", "native-unknown")),
                confidence=float(payload.get("confidence", 0.0)),
                labels_json=json.dumps(payload.get("labels", []), separators=(",", ":")),
            )
            logging.info("[native-event] Motion event %s persisted.", motion.id)
        elif event_type == "segment.finalized":
            path = Path(payload["path"])
            _recording_catalog.register(
                path,
                datetime.fromtimestamp(float(payload["start_time"])),
                datetime.fromtimestamp(float(payload["end_time"])),
                codec=str(payload.get("codec") or "hevc"),
                size=int(payload.get("size") or path.stat().st_size),
            )
            logging.info("[native-event] Indexed segment %s.", path.name)
        elif event_type == "health":
            brightness_value = payload.get("scene_brightness")
            brightness = (
                float(brightness_value)
                if isinstance(brightness_value, (int, float))
                else None
            )
            logging.info(
                "[native-health] fps=%.2f camera=%.2f output=%.2f dropped=%s "
                "encoder_dropped=%s latency_ms=%.1f motion=%.4f brightness=%s "
                "recording=%s rtsp=%s",
                float(payload.get("fps", 0)),
                float(payload.get("camera_fps", payload.get("fps", 0))),
                float(payload.get("output_fps", payload.get("fps", 0))),
                payload.get("dropped_frames", 0),
                payload.get("encoder_dropped_frames", 0),
                float(payload.get("processing_latency_ms", 0)),
                float(payload.get("motion_score", 0)),
                f"{brightness * 100:.1f}%" if brightness is not None else "unavailable",
                payload.get("recording"),
                payload.get("rtsp"),
            )
            if brightness is not None:
                decision = _resolution_controller.observe(brightness)
                if decision is not None:
                    _schedule_resolution_change(decision)
        elif event_type == "stream.disconnected":
            reason = str(payload.get("reason") or "stream closed")
            logging.warning("[native-stream] Disconnected: %s", reason)
            _resolution_controller.reset_observations()
            _cancel_post_connect_adjustments()
            _schedule_camera_recovery(reason)
        elif event_type == "stream.connected":
            logging.info("[native-stream] MJPEG signal restored.")
            _schedule_post_connect_adjustments()


def start_camera_pipeline() -> Optional[subprocess.Popen]:
    """Launch the camera pipeline if it is not already running."""
    global _camera_process, _event_reader, _active_backend

    with _camera_process_lock:
        if _camera_process and _camera_process.poll() is None:
            logging.info(
                "[orchestrator] Camera pipeline already running (pid=%s).",
                _camera_process.pid,
            )
            return _camera_process

        backend = _requested_backend()
        python_cmd = _resolve_python_command()
        command = [python_cmd, "-m", "image_processing.camera_pipeline"]
        pass_fds: tuple[int, ...] = ()
        read_fd = write_fd = None
        child_env = os.environ.copy()
        if backend == "native":
            binary = _resolve_native_binary()
            if binary is None:
                backend = "python"
            else:
                try:
                    with _camera_configuration_lock:
                        startup(target_framesize=_resolution_controller.selected_framesize)
                except CameraRecoveryMode:
                    logging.exception("[orchestrator] Camera is in recovery mode; using Python controller.")
                    backend = "python"
                except Exception:
                    logging.exception("[orchestrator] Camera startup failed; using Python fallback.")
                    backend = "python"
                if backend == "native":
                    read_fd, write_fd = os.pipe()
                    os.set_inheritable(write_fd, True)
                    child_env["CCTV_EVENT_FD"] = str(write_fd)
                    command = [str(binary)]
                    pass_fds = (write_fd,)

        logging.info("[orchestrator] Starting %s camera pipeline: %s", backend, command)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                stdin=None,
                stdout=None,
                stderr=None,
                env=child_env,
                pass_fds=pass_fds,
            )
        except Exception as exc:
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            logging.exception("[orchestrator] Failed to start camera pipeline")
            raise RuntimeError("Unable to start camera pipeline") from exc

        if write_fd is not None:
            os.close(write_fd)
        if read_fd is not None:
            reader = NativeEventReader(read_fd)
            with _event_reader_lock:
                _event_reader = reader
            reader.start()

        _camera_process = proc
        _active_backend = backend
        logging.info("[orchestrator] %s camera pipeline started (pid=%s).", backend, proc.pid)
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
        logging.warning(
            "[orchestrator] Pipeline did not exit in %.1fs, sending SIGKILL.",
            STOP_TIMEOUT_SECONDS,
        )
        try:
            proc.kill()
        except Exception:
            logging.exception("[orchestrator] Failed to SIGKILL pipeline")
        else:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logging.warning(
                    "[orchestrator] Pipeline did not terminate after SIGKILL."
                )


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

    usage = shutil.disk_usage(directory)
    target_used = int(usage.total * (target_percent / 100.0))
    bytes_to_free = max(0, usage.used - target_used)
    protect_after = datetime.now() - timedelta(minutes=10)
    _recording_catalog.reconcile(force=True)
    files = [
        (recording.start_time.timestamp(), recording.size, recording.path)
        for recording in _recording_catalog.all()
        if recording.start_time < protect_after and not recording.path.name.endswith(".partial")
    ]

    if not files:
        logging.warning("[cleanup] No video files found to delete in %s", directory)
        return

    # Sort by modification time (oldest first)
    files.sort(key=lambda x: x[0])

    deleted_count = 0
    deleted_size = 0

    deleted_paths = []
    for mtime, size, file_path in files:
        if deleted_size >= bytes_to_free:
            break
        try:
            file_path.unlink()
            deleted_paths.append(file_path)
            deleted_count += 1
            deleted_size += size
            logging.info(
                "[cleanup] Deleted old file: %s (%d MB)",
                file_path.name,
                size // (1024 * 1024),
            )
        except FileNotFoundError:
            continue
        except Exception:
            logging.exception("[cleanup] Failed to delete %s", file_path)
            continue

    _recording_catalog.remove(deleted_paths)

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
        logging.warning(
            "[cleanup] Recordings directory %s not found; skipping.", RECORDINGS_DIR
        )
        return

    try:
        usage_percent = _get_usage_percent(RECORDINGS_DIR)
    except Exception:
        logging.exception(
            "[cleanup] Unable to calculate disk usage for %s", RECORDINGS_DIR
        )
        return

    # Always log disk usage so you can see it's working
    logging.info(
        "[cleanup] Disk usage check: %s%% (threshold: %s%%)",
        usage_percent,
        DISK_USAGE_THRESHOLD,
    )

    if usage_percent < DISK_USAGE_THRESHOLD:
        return

    logging.warning(
        "[cleanup] Disk usage %s%% exceeds threshold %s%%. Starting cleanup (camera keeps running).",
        usage_percent,
        DISK_USAGE_THRESHOLD,
    )

    try:
        # Delete old files without stopping camera - already running in background thread
        _delete_oldest_files_until_threshold(RECORDINGS_DIR, DISK_USAGE_TARGET)
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
        logging.info(
            "[monitor] Storage monitor thread started (interval=%ss).", self.interval
        )
        while not self._stop_event.is_set():
            try:
                check_storage_and_cleanup()
            except Exception:
                logging.exception("[monitor] Storage cleanup failed")
            if self._stop_event.wait(self.interval):
                break
        logging.info("[monitor] Storage monitor thread stopped.")


class CameraProcessMonitor(threading.Thread):
    """Restart the camera pipeline if it exits while the orchestrator is running."""

    def __init__(self, interval_seconds: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.interval = interval_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        global _camera_process, _native_fallback_latched

        logging.info("[monitor] Camera process monitor thread started.")
        while not self._stop_event.is_set():
            with _camera_process_lock:
                proc = _camera_process

            if proc is None:
                if not _shutdown_event.is_set():
                    logging.warning("[monitor] Camera pipeline missing; starting it.")
                    start_camera_pipeline()
            else:
                return_code = proc.poll()
                if return_code is not None and not _shutdown_event.is_set():
                    logging.warning(
                        "[monitor] Camera pipeline exited with code %s; restarting.",
                        return_code,
                    )
                    with _camera_process_lock:
                        if _camera_process is proc:
                            _camera_process = None
                    if _active_backend == "native":
                        now = time.monotonic()
                        _native_failures.append(now)
                        while _native_failures and now - _native_failures[0] > NATIVE_FAILURE_WINDOW_SECONDS:
                            _native_failures.popleft()
                        if len(_native_failures) >= NATIVE_FAILURE_LIMIT:
                            _native_fallback_latched = True
                            logging.error(
                                "[monitor] Native pipeline failed %d times in %ds; latching Python fallback.",
                                len(_native_failures),
                                NATIVE_FAILURE_WINDOW_SECONDS,
                            )
                    start_camera_pipeline()

            if self._stop_event.wait(self.interval):
                break
        logging.info("[monitor] Camera process monitor thread stopped.")


def start_orchestrator() -> None:
    """Start the camera pipeline and the storage monitor."""
    global _camera_monitor, _storage_monitor

    start_camera_pipeline()

    with _camera_monitor_lock:
        if _camera_monitor is None or not _camera_monitor.is_alive():
            _camera_monitor = CameraProcessMonitor()
            _camera_monitor.start()

    with _storage_monitor_lock:
        if _storage_monitor is None or not _storage_monitor.is_alive():
            _storage_monitor = StorageMonitor(CHECK_INTERVAL_SECONDS)
            _storage_monitor.start()
            logging.info(
                "[orchestrator] Storage cleanup job scheduled every %s seconds.",
                CHECK_INTERVAL_SECONDS,
            )


def shutdown_orchestrator(sig: signal.Signals = signal.SIGTERM) -> None:
    """Stop the storage monitor and camera pipeline."""
    global _camera_monitor, _storage_monitor

    logging.info("[orchestrator] Shutting down (signal=%s).", sig.name)

    _shutdown_event.set()

    with _camera_monitor_lock:
        camera_monitor = _camera_monitor
        _camera_monitor = None

    if camera_monitor is not None:
        camera_monitor.stop()
        camera_monitor.join(timeout=STOP_TIMEOUT_SECONDS)

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

    for sig in (
        signal.SIGINT,
        signal.SIGTERM,
        getattr(signal, "SIGQUIT", signal.SIGTERM),
    ):
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
