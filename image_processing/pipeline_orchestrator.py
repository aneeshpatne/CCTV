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
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from utilities.motion_db_new import annotate_motion_event, log_motion_event
from utilities.recording_catalog import RecordingCatalog
from utilities.brightness_mode import ManualExposureController, ManualExposureDecision
from utilities.color_profile import CameraColorProfile
from utilities.floodlight import FloodlightController
from utilities.image_control import ImageControlAPIError, ImageControlClient
from utilities.white_balance_mode import (
    ManualWhiteBalanceController,
    WhiteBalanceDecision,
)
from utilities.startup import CameraRecoveryMode, startup

LOG_FORMAT = "[%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
RECORDINGS_DIR = Path(
    os.getenv("CCTV_RECORDINGS_DIR", "/Volumes/HP USB20FD/CCTV/recordings/esp_cam1")
).expanduser()
if not RECORDINGS_DIR.exists():
    RECORDINGS_DIR = REPO_ROOT / "recordings" / "esp_cam1"
DISK_USAGE_THRESHOLD = 90  # percent
DISK_USAGE_TARGET = 85  # cleanup hysteresis target
CHECK_INTERVAL_SECONDS = 5 * 60
STOP_TIMEOUT_SECONDS = 5.0
NATIVE_FAILURE_LIMIT = 3
NATIVE_FAILURE_WINDOW_SECONDS = 5 * 60
CAMERA_BASE_URL = os.getenv("ESP32CAM_BASE_URL", "http://192.168.0.13").rstrip("/")

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
_camera_configuration_lock = threading.Lock()
_image_control_state_lock = threading.Lock()
_image_control_ready = threading.Event()
_image_control_generation = 0
_exposure_adjustment_lock = threading.Lock()
_exposure_adjustment_thread: Optional[threading.Thread] = None
_white_balance_adjustment_lock = threading.Lock()
_white_balance_adjustment_thread: Optional[threading.Thread] = None
_wb_drift_check_lock = threading.Lock()
_wb_drift_check_thread: Optional[threading.Thread] = None
_wb_drift_last_check = 0.0
_WB_DRIFT_CHECK_INTERVAL_SECONDS = float(
    os.getenv("CCTV_WB_DRIFT_CHECK_INTERVAL_SECONDS", "20")
)
_image_control = ImageControlClient(CAMERA_BASE_URL)
_exposure_controller = ManualExposureController.from_environment()
_white_balance_controller = ManualWhiteBalanceController.from_environment()
_floodlight = FloodlightController.from_environment()
try:
    _color_profile = CameraColorProfile.load()
    _color_profile_error: str | None = None
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
    _color_profile = None
    _color_profile_error = str(error)


def _disable_for_image_error(error: ImageControlAPIError) -> bool:
    return error.status in {400, 413, 500, 501} or error.code == "invalid_response"


def _begin_image_control_recovery() -> int:
    global _image_control_generation
    with _image_control_state_lock:
        _image_control_generation += 1
        generation = _image_control_generation
        _image_control_ready.clear()
    _exposure_controller.invalidate_pending()
    _white_balance_controller.invalidate_pending()
    return generation


def _current_image_control_generation() -> int:
    with _image_control_state_lock:
        return _image_control_generation


def _generation_is_current(generation: int) -> bool:
    return generation == _current_image_control_generation()


def _validate_authoritative_profile(
    profile: dict,
    *,
    expected_white_balance: dict[str, int] | None = None,
) -> None:
    exposure = profile.get("exposure")
    white_balance = profile.get("whiteBalance")
    if not isinstance(exposure, dict):
        raise ValueError("image profile is missing exposure")
    if exposure.get("autoExposure") is not False or exposure.get("autoGain") is not False:
        raise ValueError("image profile does not have manual AE/AGC")
    if not isinstance(white_balance, dict) or white_balance.get("auto") is not False:
        raise ValueError("image profile does not have manual white balance")
    if profile.get("cachedForRecovery") is not True:
        raise ValueError("manual image profile is not cached for recovery")
    if expected_white_balance is not None:
        actual = {name: int(white_balance[name]) for name in ("red", "green", "blue")}
        if actual != expected_white_balance:
            raise ValueError(f"white-balance read-back mismatch: {actual}")
    if _color_profile is not None:
        saturation = profile.get("color", {}).get("saturation", {})
        expected_saturation = {
            "u": _color_profile.saturation_u,
            "v": _color_profile.saturation_v,
        }
        actual_saturation = {name: int(saturation[name]) for name in ("u", "v")}
        if actual_saturation != expected_saturation:
            raise ValueError(f"saturation read-back mismatch: {actual_saturation}")
        tone = profile.get("tone") or {}
        if int(tone.get("lumaOffset", 0)) != _color_profile.luma_offset:
            raise ValueError(
                f"lumaOffset read-back mismatch: {tone.get('lumaOffset')}"
            )
        actual_contrast = [int(value) for value in tone.get("contrastRegisters", [])]
        expected_contrast = list(_color_profile.contrast_registers)
        if actual_contrast != expected_contrast:
            raise ValueError(
                f"contrastRegisters read-back mismatch: {actual_contrast}"
            )


def _initialize_manual_exposure(generation: int | None = None) -> dict:
    """Apply and verify all manual image groups before enabling controllers."""
    generation = _current_image_control_generation() if generation is None else generation
    if _color_profile is None:
        raise ValueError(f"invalid color profile: {_color_profile_error}")

    baseline_white_balance = {
        "red": _color_profile.red,
        "green": _color_profile.green,
        "blue": _color_profile.blue,
    }
    # Oneshot mode re-seeds from the profile baseline so each recovery can look at
    # the live scene and pick values once. Continuous mode restores last verified.
    if getattr(_white_balance_controller, "mode", None) == "oneshot":
        requested_white_balance = baseline_white_balance
    else:
        try:
            saved = _white_balance_controller.saved_white_balance(baseline_white_balance)
            requested_white_balance = saved or baseline_white_balance
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            logging.warning("[image-control] Ignoring invalid WB state: %s", error)
            requested_white_balance = baseline_white_balance

    last_error: Exception | None = None
    for attempt in range(1, 4):
        if not _generation_is_current(generation):
            raise RuntimeError("camera generation changed during image-control recovery")
        try:
            profile = _image_control.freeze_exposure(_image_control.get_profile())
            normalization = _exposure_controller.initialize(profile)
            if normalization is not None:
                profile = _image_control.update_exposure(
                    normalization["shutterLines"], normalization["gainX16"]
                )
            profile = _image_control.update_profile(
                {"whiteBalance": {"auto": False, **requested_white_balance}}
            )
            _image_control.update_profile(_color_profile.saturation_patch())
            _image_control.update_profile(_color_profile.tone_patch())
            profile = _image_control.get_profile()
            _validate_authoritative_profile(
                profile, expected_white_balance=requested_white_balance
            )
            _exposure_controller.initialize(profile)
            _white_balance_controller.initialize(profile, baseline_white_balance)
            if not _generation_is_current(generation):
                raise RuntimeError("camera generation changed after image-control verification")
            _image_control_ready.set()
            break
        except (ImageControlAPIError, KeyError, TypeError, ValueError) as error:
            last_error = error
            _image_control_ready.clear()
            logging.warning(
                "[image-control] Reconciliation attempt %d/3 failed: %s", attempt, error
            )
            if attempt < 3:
                time.sleep(float(attempt))
    else:
        raise RuntimeError(f"unable to verify manual image control: {last_error}")

    logging.info(
        "[image-control] %s · %s",
        _exposure_controller.status_summary(),
        _white_balance_controller.status_summary(),
    )
    return profile


def _schedule_camera_recovery(reason: str) -> None:
    """Run the full ESP startup loop once while native output shows no signal."""
    global _camera_recovery_thread

    with _camera_recovery_lock:
        if _shutdown_event.is_set():
            return
        if _camera_recovery_thread and _camera_recovery_thread.is_alive():
            logging.info("[recovery] Camera startup already running; coalescing disconnect (%s).", reason)
            return

        generation = _begin_image_control_recovery()

        def recover() -> None:
            logging.warning("[recovery] MJPEG disconnected (%s); running full camera startup.", reason)
            while not _shutdown_event.is_set() and _generation_is_current(generation):
                try:
                    with _camera_configuration_lock:
                        startup()
                        _initialize_manual_exposure(generation)
                except CameraRecoveryMode:
                    logging.exception("[recovery] Camera entered OTA/recovery mode during startup.")
                    return
                except Exception:
                    logging.exception(
                        "[recovery] Camera startup or manual verification failed; retrying in 5s."
                    )
                    _shutdown_event.wait(5)
                else:
                    logging.info("[recovery] Camera startup and manual image verification completed.")
                    return

        _camera_recovery_thread = threading.Thread(
            target=recover,
            daemon=True,
            name="cctv-camera-recovery",
        )
        _camera_recovery_thread.start()


def _verify_profile_after_connect() -> None:
    """Read back manual state after a connection transition without blocking events."""
    with _camera_recovery_lock:
        if _camera_recovery_thread and _camera_recovery_thread.is_alive():
            return

    def verify() -> None:
        try:
            with _camera_configuration_lock:
                profile = _image_control.get_profile()
                _validate_authoritative_profile(profile)
        except (ImageControlAPIError, KeyError, TypeError, ValueError) as error:
            logging.warning("[image-control] Post-connect verification failed: %s", error)
            _schedule_camera_recovery(f"post-connect verification failed: {error}")

    threading.Thread(
        target=verify,
        daemon=True,
        name="cctv-image-control-verifier",
    ).start()


def _schedule_exposure_adjustment(decision: ManualExposureDecision) -> None:
    """Apply one manual exposure correction without blocking native event reads."""
    global _exposure_adjustment_thread

    generation = _current_image_control_generation()
    with _exposure_adjustment_lock:
        if _shutdown_event.is_set() or not _image_control_ready.is_set():
            _exposure_controller.complete(None, success=False)
            return
        if _exposure_adjustment_thread and _exposure_adjustment_thread.is_alive():
            return

        def adjust() -> None:
            if not _generation_is_current(generation):
                _exposure_controller.complete(None, success=False)
                return
            logging.info(
                "[image-control] %.1f%% brightness: %s correction to %dL, gain %d/16.",
                decision.average_brightness * 100,
                decision.direction,
                decision.shutter_lines,
                decision.gain_x16,
            )
            try:
                with _camera_configuration_lock:
                    if (
                        not _image_control_ready.is_set()
                        or not _generation_is_current(generation)
                    ):
                        _exposure_controller.complete(None, success=False)
                        return
                    profile = _image_control.update_exposure(
                        decision.shutter_lines, decision.gain_x16
                    )
                    _validate_authoritative_profile(profile)
                _exposure_controller.complete(profile, success=True)
                _white_balance_controller.sync_profile(profile)
                _white_balance_controller.hold()
                logging.info("[image-control] Applied %s", _exposure_controller.status_summary())
            except (ImageControlAPIError, ValueError) as error:
                _exposure_controller.complete(None, success=False)
                logging.exception("[image-control] Manual exposure correction failed")
                _schedule_camera_recovery(f"manual exposure verification failed: {error}")

        _exposure_adjustment_thread = threading.Thread(
            target=adjust,
            daemon=True,
            name="cctv-manual-exposure",
        )
        _exposure_adjustment_thread.start()


def _schedule_white_balance_adjustment(decision: WhiteBalanceDecision) -> None:
    """Apply one software WB correction without enabling sensor AWB."""
    global _white_balance_adjustment_thread

    generation = _current_image_control_generation()
    with _white_balance_adjustment_lock:
        if _shutdown_event.is_set() or not _image_control_ready.is_set():
            _white_balance_controller.complete(None, success=False)
            return
        if _white_balance_adjustment_thread and _white_balance_adjustment_thread.is_alive():
            return

        def adjust() -> None:
            if not _generation_is_current(generation):
                _white_balance_controller.complete(None, success=False)
                return
            logging.info(
                "[image-control] WB %s from chroma %s/1/%s to %d/%d/%d.",
                decision.action,
                "--" if decision.average_red_over_green is None else f"{decision.average_red_over_green:.3f}",
                "--" if decision.average_blue_over_green is None else f"{decision.average_blue_over_green:.3f}",
                decision.red,
                decision.green,
                decision.blue,
            )
            try:
                with _camera_configuration_lock:
                    if (
                        not _image_control_ready.is_set()
                        or not _generation_is_current(generation)
                    ):
                        _white_balance_controller.complete(None, success=False)
                        return
                    profile = _image_control.update_profile(
                        {
                            "whiteBalance": {
                                "auto": False,
                                "red": decision.red,
                                "green": decision.green,
                                "blue": decision.blue,
                            }
                        }
                    )
                    _validate_authoritative_profile(
                        profile,
                        expected_white_balance={
                            "red": decision.red,
                            "green": decision.green,
                            "blue": decision.blue,
                        },
                    )
                # Validate the complete returned profile before either controller
                # commits it or the state store persists it.
                _exposure_controller.complete(profile, success=True)
                _white_balance_controller.complete(profile, success=True)
                logging.info("[image-control] Applied %s", _white_balance_controller.status_summary())
            except (ImageControlAPIError, ValueError) as error:
                _white_balance_controller.complete(None, success=False)
                logging.exception("[image-control] Manual WB correction failed")
                _schedule_camera_recovery(f"manual WB verification failed: {error}")

        _white_balance_adjustment_thread = threading.Thread(
            target=adjust,
            daemon=True,
            name="cctv-manual-white-balance",
        )
        _white_balance_adjustment_thread.start()


def _schedule_wb_drift_check() -> None:
    """Read camera RGB; if it drifted from the locked set, restore and re-open oneshot."""
    global _wb_drift_check_thread, _wb_drift_last_check

    now = time.monotonic()
    with _wb_drift_check_lock:
        if _shutdown_event.is_set() or not _image_control_ready.is_set():
            return
        if now - _wb_drift_last_check < _WB_DRIFT_CHECK_INTERVAL_SECONDS:
            return
        if _wb_drift_check_thread and _wb_drift_check_thread.is_alive():
            return
        _wb_drift_last_check = now
        generation = _current_image_control_generation()

        def check() -> None:
            if not _generation_is_current(generation):
                return
            try:
                with _camera_configuration_lock:
                    if (
                        not _image_control_ready.is_set()
                        or not _generation_is_current(generation)
                    ):
                        return
                    profile = _image_control.get_profile()
                    white_balance = profile.get("whiteBalance") or {}
                    restore = _white_balance_controller.check_camera_drift(
                        int(white_balance["red"]),
                        int(white_balance["green"]),
                        int(white_balance["blue"]),
                    )
                    if restore is None:
                        return
                    logging.warning(
                        "[image-control] Camera WB drifted to %s/%s/%s; restoring %d/%d/%d and reopening verify.",
                        white_balance.get("red"),
                        white_balance.get("green"),
                        white_balance.get("blue"),
                        restore["red"],
                        restore["green"],
                        restore["blue"],
                    )
                    profile = _image_control.update_profile(
                        {"whiteBalance": {"auto": False, **restore}}
                    )
                    _validate_authoritative_profile(
                        profile, expected_white_balance=restore
                    )
                _exposure_controller.complete(profile, success=True)
                _white_balance_controller.sync_profile(profile)
                # Leave unlocked (oneshot HOLD) so the next bright-frame window
                # can re-enter ADJUST/VERIFY and lock again.
                logging.info(
                    "[image-control] Drift restore applied · %s",
                    _white_balance_controller.status_summary(),
                )
            except (ImageControlAPIError, KeyError, TypeError, ValueError):
                logging.exception("[image-control] WB drift check failed")

        _wb_drift_check_thread = threading.Thread(
            target=check,
            daemon=True,
            name="cctv-wb-drift-check",
        )
        _wb_drift_check_thread.start()


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
            _schedule_wb_drift_check()
        elif event_type == "image.metrics":
            if not _image_control_ready.is_set():
                return
            brightness_value = payload.get("scene_brightness")
            brightness = (
                float(brightness_value)
                if isinstance(brightness_value, (int, float))
                else None
            )
            floodlight_transition = _floodlight.observe(brightness)
            if floodlight_transition or _floodlight.image_adjustments_paused:
                _exposure_controller.reset_observations()
                _white_balance_controller.hold()
                return
            if isinstance(brightness_value, (int, float)):
                decision = _exposure_controller.observe(float(brightness_value))
                if decision is not None:
                    _white_balance_controller.hold()
                    _schedule_exposure_adjustment(decision)
                    return
            red_ratio = payload.get("red_over_green")
            blue_ratio = payload.get("blue_over_green")
            wb_decision = _white_balance_controller.observe(
                float(red_ratio) if isinstance(red_ratio, (int, float)) else None,
                float(blue_ratio) if isinstance(blue_ratio, (int, float)) else None,
                scene_brightness=(
                    float(brightness_value)
                    if isinstance(brightness_value, (int, float))
                    else None
                ),
            )
            if wb_decision is not None:
                _schedule_white_balance_adjustment(wb_decision)
            _schedule_wb_drift_check()
        elif event_type == "motion.started":
            _floodlight.motion_started()
        elif event_type == "stream.disconnected":
            reason = str(payload.get("reason") or "stream closed")
            logging.warning("[native-stream] Disconnected: %s", reason)
            _schedule_camera_recovery(reason)
        elif event_type == "stream.connected":
            logging.info("[native-stream] MJPEG signal restored.")
            _verify_profile_after_connect()


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
                        generation = _begin_image_control_recovery()
                        startup()
                        _initialize_manual_exposure(generation)
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
    _floodlight.close()


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
