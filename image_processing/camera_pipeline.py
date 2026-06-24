from __future__ import annotations

import os

# CRITICAL: Set FFMPEG options BEFORE importing cv2
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "protocol_whitelist;file,http,https,tcp|"
    "timeout;2500000|"
    "rw_timeout;2500000|"
    "analyzeduration;0|"
    "probesize;32|"  # tiny probe
    "fflags;nobuffer|"  # minimize internal buffering
    "flags;low_delay|"  # lower latency
    "max_delay;0|"  # no queuing delay
)

import threading
import time
import signal
import subprocess
import requests
import sys
from datetime import datetime
from typing import Optional
from pathlib import Path

import cv2
import numpy as np
import pytz
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None
from utilities.startup import CameraRecoveryMode, startup
from utilities.esp32cam_client import get_camera_status_with_retry
from utilities.warn import NonBlockingBlinker
from tools.get_rssi import get_rssi
from tools.mjpeg_capture import MJPEG_STREAM_URL, MjpegStreamCapture
from tools.reset import reset
from utilities.EventAccumulator import EventAccumulator
from utilities.motion_db_new import log_motion_event

IST = pytz.timezone("Asia/Kolkata")
NO_SIGNAL_PATH = os.path.join(os.path.dirname(__file__), "examples", "no_signal.png")
FRAME_RETRY_DELAY = 0.5
FRAME_READ_TIMEOUT = 5.0  # seconds
FRAME_FAILURE_RECONNECT_DELAY = 2.0  # seconds to let the ESP32 close the old stream
CAPTURE_OPEN_TIMEOUT = 10.0  # seconds to wait for HTTP stream open
STREAM_STARTUP_FAILURE_THRESHOLD = 3
CAMERA_STARTUP_COOLDOWN = 60.0
BLINKER_COOLDOWN = 3.0  # seconds between blinker activations (debounce)

# Recording configuration
ENABLE_RECORDING = True
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDINGS_DIR = REPO_ROOT / "recordings" / "esp_cam1"
PRIMARY_RECORDINGS_DIR = Path(
    os.getenv("CCTV_RECORDINGS_DIR", "/Volumes/drive/CCTV/recordings/esp_cam1")
).expanduser()
try:
    BASE_DIR = PRIMARY_RECORDINGS_DIR
    BASE_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    BASE_DIR = DEFAULT_RECORDINGS_DIR
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Warning: Primary recording path unavailable, using: {BASE_DIR}")

SEGMENT_SECONDS = 60  # 1 minute per segment
RTSP_OUT = "rtsp://127.0.0.1:8554/esp_cam1_overlay"
ENABLE_RTSP = True  # Set to True if you want RTSP streaming
USE_DYNAMIC_FPS = False  # Use fixed output FPS for stream stability
FIXED_OUTPUT_FPS = 9.0
VIDEO_BITRATE_KBPS = 1500
VIDEO_BUFSIZE_KBPS = 3000
FFMPEG_LOG_MAX_BYTES = 20 * 1024 * 1024
CCTV_H264_ENCODER = os.getenv("CCTV_H264_ENCODER", "auto").lower()
USE_APPLE_VIDEOTOOLBOX = sys.platform == "darwin" and CCTV_H264_ENCODER != "libx264"
_videotoolbox_failed = False

# Display configuration
SHOW_MOTION_BOXES = False  # Show motion detection boxes and ROI polygon
SHOW_LOCAL_VIEW = False  # Show CV2 preview windows
SHOW_TEMPERATURE_BADGE = True  # Show ESP32 SoC temperature badge
PROJECT_LEXEND_FONT = REPO_ROOT / "font" / "Lexend-VariableFont_wght.ttf"
PROJECT_ROBOTO_FONT = REPO_ROOT / "assets" / "fonts" / "Roboto-Regular.ttf"
DEFAULT_HUD_FONT_PATH = next(
    (
        font_path
        for font_path in (PROJECT_LEXEND_FONT, PROJECT_ROBOTO_FONT, Path("/System/Library/Fonts/SFNS.ttf"))
        if font_path.exists()
    ),
    Path("/System/Library/Fonts/SFNS.ttf"),
)
HUD_FONT_PATH = os.getenv("CCTV_HUD_FONT", str(DEFAULT_HUD_FONT_PATH))

# Motion detection configuration
MIN_AREA = 800
ROI_PTS = np.array(
    [
        [12, 5],
        [34, 4],
        [69, 1],
        [94, 3],
        [122, 10],
        [137, 3],
        [161, 21],
        [178, 55],
        [188, 74],
        [218, 64],
        [242, 60],
        [260, 59],
        [299, 58],
        [340, 66],
        [393, 71],
        [432, 74],
        [461, 72],
        [489, 67],
        [515, 63],
        [561, 66],
        [617, 88],
        [660, 91],
        [732, 90],
        [765, 76],
        [780, 71],
        [815, 58],
        [818, 35],
        [814, 16],
        [845, 7],
        [873, 10],
        [920, 9],
        [949, 14],
        [985, 14],
        [1009, 13],
        [1020, 43],
        [1021, 71],
        [1018, 98],
        [1023, 130],
        [1023, 154],
        [1016, 194],
        [1021, 241],
        [1023, 323],
        [1023, 333],
        [1018, 354],
        [1020, 502],
        [1020, 559],
        [1017, 606],
        [1023, 676],
        [1016, 720],
        [1015, 756],
        [967, 761],
        [923, 758],
        [873, 761],
        [805, 765],
        [730, 752],
        [687, 757],
        [570, 754],
        [478, 755],
        [424, 750],
        [354, 749],
        [282, 755],
        [219, 757],
        [129, 752],
        [87, 753],
        [46, 746],
        [14, 742],
        [9, 697],
        [11, 641],
        [12, 598],
        [11, 553],
        [12, 506],
        [12, 441],
        [9, 377],
        [13, 319],
        [13, 259],
        [6, 199],
        [13, 117],
        [11, 61],
        [10, 8],
    ],
    dtype=np.int32,
)

no_signal_img = cv2.imread(NO_SIGNAL_PATH)
if no_signal_img is None:
    print(f"Warning: Could not load no_signal.png from {NO_SIGNAL_PATH}")
    no_signal_img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(
        no_signal_img,
        "NO SIGNAL",
        (160, 260),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.4,
        (0, 0, 255),
        3,
        cv2.LINE_AA,
    )

startup_complete = threading.Event()
startup_thread = None
device_state = "offline"
device_state_detail = "waiting for first status check"
device_state_lock = threading.Lock()
startup_lock = threading.Lock()

# Camera adjustment state (run after stream starts)
camera_adjustments_done = False
camera_adjustments_lock = threading.Lock()

# RSSI monitoring state
rssi_value = None
rssi_lock = threading.Lock()
rssi_thread = None
rssi_update_interval = 10  # Update RSSI every 10 seconds

# Temperature monitoring state
soc_temperature_c = None
temperature_lock = threading.Lock()
temperature_thread = None
temperature_update_interval = 10  # Update temperature every 10 seconds

# FPS tracking state
fps_value = 0.0
fps_lock = threading.Lock()
fps_frame_times = []
FPS_SAMPLE_WINDOW = 30  # Calculate FPS over last 30 frames

# HUD overlap cooldown configuration
HUD_HIDE_SECONDS = 5.0

class BoxVisibilityCooldown:
    """Tracks temporary hide windows for HUD boxes after overlap events."""

    def __init__(self) -> None:
        self._hide_until: dict[str, float] = {}

    def set_hidden(self, key: str, now: float, seconds: float) -> None:
        hide_until = now + seconds
        current = self._hide_until.get(key, 0.0)
        if hide_until > current:
            self._hide_until[key] = hide_until

    def is_hidden(self, key: str, now: float) -> bool:
        hide_until = self._hide_until.get(key)
        if hide_until is None:
            return False
        if now >= hide_until:
            del self._hide_until[key]
            return False
        return True


HUD_COOLDOWN = BoxVisibilityCooldown()


def _save_accumulated_motion_event(event: dict[str, float]) -> None:
    """Persist an EventAccumulator event using the new motion DB schema."""
    start_ts = event.get("start_time")
    end_ts = event.get("end_time")
    duration = event.get("duration")
    if start_ts is None or end_ts is None or duration is None:
        return
    try:
        log_motion_event(
            start_time=datetime.fromtimestamp(start_ts),
            end_time=datetime.fromtimestamp(end_ts),
            duration=float(duration),
        )
    except Exception as e:
        print(f"Failed to log motion event: {e}")


# Accumulator For Event Tracking
acc = EventAccumulator(cooldown=15, onSave=_save_accumulated_motion_event)

# Recording state
ffmpeg_record_proc: Optional[subprocess.Popen] = None
ffmpeg_rtsp_proc: Optional[subprocess.Popen] = None
ffmpeg_lock = threading.Lock()
expected_frame_size: Optional[tuple[int, int]] = (
    None  # (width, height) that FFmpeg expects
)
current_fps: Optional[float] = None  # Active FPS used by FFmpeg
last_record_write_time: Optional[float] = None
MAX_RECORD_FRAME_DUPLICATES = 30
FFMPEG_RESTART_COOLDOWN_SECONDS = 5.0
last_record_restart_attempt: Optional[float] = None
last_rtsp_restart_attempt: Optional[float] = None


def _using_videotoolbox() -> bool:
    return USE_APPLE_VIDEOTOOLBOX and not _videotoolbox_failed


def _mark_videotoolbox_failed(reason: str) -> None:
    global _videotoolbox_failed
    if _using_videotoolbox() and CCTV_H264_ENCODER != "videotoolbox":
        _videotoolbox_failed = True
        print(f"VideoToolbox encoder failed ({reason}); falling back to libx264.")


def _h264_encoder_args(realtime: bool) -> list[str]:
    """Return FFmpeg H.264 encoder args for the current platform."""
    if _using_videotoolbox():
        args = [
            "-vf",
            "format=nv12",
            "-c:v",
            "h264_videotoolbox",
            "-allow_sw",
            "0",
            "-power_efficient",
            "1",
        ]
        if realtime:
            args.extend(["-realtime", "1", "-prio_speed", "1"])
        return args

    args = [
        "-vf",
        "format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast" if realtime else "medium",
    ]
    if realtime:
        args.extend(["-tune", "zerolatency"])
    return args


def open_ffmpeg_log(log_path: Path):
    """Open an FFmpeg stderr log, truncating stale multi-GB progress logs."""
    try:
        if log_path.exists() and log_path.stat().st_size > FFMPEG_LOG_MAX_BYTES:
            with open(log_path, "wb", buffering=0) as logf:
                logf.write(b"[log truncated after exceeding size cap]\n")
    except OSError as exc:
        print(f"Warning: could not truncate FFmpeg log {log_path}: {exc}")
    return open(log_path, "ab", buffering=0)


def start_ffmpeg_record(
    width: int, height: int, fps: float
) -> Optional[subprocess.Popen]:
    """Start FFmpeg process for variable frame rate CCTV recording."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    out_pattern = BASE_DIR / "recording_%Y%m%d_%H%M%S.mp4"
    safe_fps = max(1.0, fps)
    gop_size = max(1, int(round(safe_fps)))

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-y",
        # raw frames over stdin
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{safe_fps:.2f}",  # Match input cadence to measured FPS
        "-use_wallclock_as_timestamps",
        "1",
        "-i",
        "-",
        "-map",
        "0:v",
        *_h264_encoder_args(realtime=False),
        # stable quality (avoid blur/clear cycling)
        "-b:v",
        f"{VIDEO_BITRATE_KBPS}k",
        "-maxrate",
        f"{VIDEO_BITRATE_KBPS}k",
        "-bufsize",
        f"{VIDEO_BUFSIZE_KBPS}k",
        # GOP: ~1 second for smoother HLS segment cadence
        "-g",
        str(gop_size),
        "-bf",
        "0",
        # segmenting
        "-f",
        "segment",
        "-segment_time",
        str(SEGMENT_SECONDS),
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+faststart",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        out_pattern,
    ]

    # ---- Spawn process with logging -------------------------------------------
    try:
        log_path = BASE_DIR / "ffmpeg_record.log"
        logf = open_ffmpeg_log(log_path)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=logf,  # keep stderr for diagnostics
            bufsize=0,
        )
        encoder = "h264_videotoolbox" if _using_videotoolbox() else "libx264"
        print(f"FFmpeg VFR recording started with {encoder}: {out_pattern}")
        return proc
    except Exception as e:
        print(f"Failed to start FFmpeg: {e}")
        return None


def start_ffmpeg_rtsp(
    width: int, height: int, fps: float
) -> Optional[subprocess.Popen]:
    """Start FFmpeg process for variable frame rate RTSP restream."""
    safe_fps = max(1.0, fps)
    gop_size = max(1, int(round(safe_fps)))
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-y",
        # Raw frames from Python
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{safe_fps:.2f}",  # Match input cadence to measured FPS
        "-use_wallclock_as_timestamps",
        "1",
        "-i",
        "-",
        "-map",
        "0:v",
        *_h264_encoder_args(realtime=True),
        # Stable bitrate (no pulsing)
        "-b:v",
        f"{VIDEO_BITRATE_KBPS}k",
        "-maxrate",
        f"{VIDEO_BITRATE_KBPS}k",
        "-bufsize",
        f"{VIDEO_BUFSIZE_KBPS}k",
        # GOP / latency
        "-g",
        str(gop_size),  # ~1 second of frames
        "-bf",
        "0",
        # RTSP output
        "-rtsp_transport",
        "tcp",
        "-pkt_size",
        "1200",
        "-f",
        "rtsp",
        RTSP_OUT,
    ]

    try:
        log_path = BASE_DIR / "ffmpeg_rtsp.log"
        logf = open_ffmpeg_log(log_path)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=logf,
            bufsize=0,
        )
        encoder = "h264_videotoolbox" if _using_videotoolbox() else "libx264"
        print(f"FFmpeg VFR RTSP started with {encoder}: {RTSP_OUT}")
        return proc
    except Exception as e:
        print(f"Failed to start FFmpeg RTSP: {e}")
        return None


def stop_ffmpeg(proc: Optional[subprocess.Popen]) -> None:
    """Stop FFmpeg process gracefully."""
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=3)
        print("FFmpeg recording stopped")
    except Exception as e:
        print(f"Error stopping FFmpeg: {e}")
        try:
            proc.kill()
        except Exception:
            pass


def write_frame_to_ffmpeg(frame: np.ndarray) -> bool:
    """Push a frame into the recording/RTSP FFmpeg pipelines, restarting them when needed."""
    global ffmpeg_record_proc, ffmpeg_rtsp_proc, expected_frame_size, current_fps, last_record_write_time
    global last_record_restart_attempt, last_rtsp_restart_attempt

    if not ENABLE_RECORDING and not ENABLE_RTSP:
        return True

    with ffmpeg_lock:
        h, w = frame.shape[:2]
        new_size = (w, h)

        # Get current FPS from the FPS tracker
        with fps_lock:
            measured_fps = fps_value if fps_value > 0 else FIXED_OUTPUT_FPS

        # Check if we need to restart FFmpeg due to size or FPS change
        fps_changed = False
        if USE_DYNAMIC_FPS and current_fps is not None:
            # Restart if FPS changes by more than 1 FPS to avoid constant restarts from small fluctuations
            if abs(measured_fps - current_fps) > 1.0:
                fps_changed = True
                print(
                    f"FPS changed from {current_fps:.2f} to {measured_fps:.2f}; restarting FFmpeg pipelines."
                )

        # Track the canonical size expected by the encoders
        if expected_frame_size is None:
            expected_frame_size = new_size
            current_fps = measured_fps if USE_DYNAMIC_FPS else FIXED_OUTPUT_FPS
        elif new_size != expected_frame_size or fps_changed:
            if new_size != expected_frame_size:
                print(
                    f"Frame size changed from {expected_frame_size[0]}x{expected_frame_size[1]} to {w}x{h}; "
                    "restarting FFmpeg pipelines."
                )
            if ffmpeg_record_proc is not None:
                stop_ffmpeg(ffmpeg_record_proc)
                ffmpeg_record_proc = None
                last_record_write_time = None
            if ffmpeg_rtsp_proc is not None:
                stop_ffmpeg(ffmpeg_rtsp_proc)
                ffmpeg_rtsp_proc = None
            expected_frame_size = new_size
            current_fps = measured_fps if USE_DYNAMIC_FPS else FIXED_OUTPUT_FPS

        target_width, target_height = expected_frame_size
        if USE_DYNAMIC_FPS:
            target_fps = current_fps if current_fps is not None else measured_fps
        else:
            target_fps = FIXED_OUTPUT_FPS

        def _can_restart(label: str) -> bool:
            last_attempt = (
                last_record_restart_attempt
                if label == "recording"
                else last_rtsp_restart_attempt
            )
            if last_attempt is None:
                return True
            return time.monotonic() - last_attempt >= FFMPEG_RESTART_COOLDOWN_SECONDS

        def _start_with_backoff(label: str, starter) -> Optional[subprocess.Popen]:
            global last_record_restart_attempt, last_rtsp_restart_attempt
            if not _can_restart(label):
                return None
            now = time.monotonic()
            if label == "recording":
                last_record_restart_attempt = now
            else:
                last_rtsp_restart_attempt = now
            return starter(target_width, target_height, target_fps)

        # Ensure recording process is alive when recording enabled
        if ENABLE_RECORDING:
            if ffmpeg_record_proc is not None and ffmpeg_record_proc.poll() is not None:
                exit_code = ffmpeg_record_proc.poll()
                print(f"Recording FFmpeg exited (code {exit_code}); restarting...")
                _mark_videotoolbox_failed(f"recording exited with code {exit_code}")
                stop_ffmpeg(ffmpeg_record_proc)
                ffmpeg_record_proc = None
                last_record_write_time = None
            if ffmpeg_record_proc is None:
                ffmpeg_record_proc = _start_with_backoff(
                    "recording", start_ffmpeg_record
                )
                last_record_write_time = None

        # Ensure RTSP process is alive when enabled
        if ENABLE_RTSP:
            if ffmpeg_rtsp_proc is not None and ffmpeg_rtsp_proc.poll() is not None:
                exit_code = ffmpeg_rtsp_proc.poll()
                print(f"RTSP FFmpeg exited (code {exit_code}); restarting...")
                stop_ffmpeg(ffmpeg_rtsp_proc)
                ffmpeg_rtsp_proc = None
            if ffmpeg_rtsp_proc is None:
                ffmpeg_rtsp_proc = _start_with_backoff("rtsp", start_ffmpeg_rtsp)

        # If the current frame size differs from the expected size, resize once for both outputs
        if (w, h) != expected_frame_size:
            frame = cv2.resize(frame, expected_frame_size)

        frame_bytes = frame.tobytes()

        def _record_frame_copies() -> int:
            global last_record_write_time
            now = time.monotonic()
            if last_record_write_time is None:
                last_record_write_time = now
                return 1

            frame_interval = 1.0 / max(1.0, target_fps)
            elapsed = max(0.0, now - last_record_write_time)
            copies = max(1, int(round(elapsed / frame_interval)))
            copies = min(copies, MAX_RECORD_FRAME_DUPLICATES)
            last_record_write_time += copies * frame_interval
            if last_record_write_time > now:
                last_record_write_time = now
            return copies

        def _write(
            proc: Optional[subprocess.Popen], label: str, starter
        ) -> Optional[subprocess.Popen]:
            global last_record_write_time
            if proc is None:
                return None
            try:
                if proc.stdin:
                    copies = _record_frame_copies() if label == "recording" else 1
                    for _ in range(copies):
                        proc.stdin.write(frame_bytes)
            except (BrokenPipeError, IOError) as err:
                print(f"FFmpeg {label} pipe error ({err}); restarting...")
                if label == "recording":
                    _mark_videotoolbox_failed(f"{label} pipe error")
                stop_ffmpeg(proc)
                if label == "recording":
                    last_record_write_time = None
                return _start_with_backoff(label, starter)
            return proc

        if ENABLE_RECORDING:
            ffmpeg_record_proc = _write(
                ffmpeg_record_proc, "recording", start_ffmpeg_record
            )
        if ENABLE_RTSP:
            ffmpeg_rtsp_proc = _write(ffmpeg_rtsp_proc, "rtsp", start_ffmpeg_rtsp)

        return True


def set_device_state(state: str, detail: str = "") -> None:
    global device_state, device_state_detail
    with device_state_lock:
        device_state = state
        device_state_detail = detail


def get_device_state_message(prefix: str, attempt: int | None = None) -> str:
    with device_state_lock:
        state = device_state
        detail = device_state_detail

    if state == "ota-only":
        base = "RECOVERY: OTA-only mode"
    elif state == "wifi-online-camera-starting":
        base = "STARTUP: WiFi online, camera server starting"
    elif state == "camera-online":
        base = "STREAM: Camera HTTP status online"
    elif state == "offline":
        base = "OFFLINE: Device unreachable"
    else:
        base = f"{prefix}: {state}"

    if attempt is not None:
        base = f"{base} | stream attempt {attempt}"
    if detail:
        base = f"{base}\n{detail}"
    return base


def is_device_ota_only() -> bool:
    with device_state_lock:
        return device_state == "ota-only"


def status_detail(status) -> str:
    details = []
    if status.source:
        details.append(f"status={status.source}")
    if status.mode:
        details.append(f"mode={status.mode}")
    if status.framesize is not None:
        details.append(f"framesize={status.framesize}")
    if status.reason:
        details.append(f"reason={status.reason}")
    if status.bad_boot_count is not None:
        details.append(f"badBootCount={status.bad_boot_count}")
    return ", ".join(details)


def exit_ota_recovery_and_check() -> bool:
    set_device_state(
        "restarting",
        "clearing recovery flag in Redis, then sending ESP32 reset",
    )
    if not reset():
        set_device_state(
            "ota-only",
            "failed to clear Redis recovery flag or send reset; manual intervention required",
        )
        return False

    set_device_state(
        "restarting",
        "reset sent; waiting 10 seconds before checking /status-v2",
    )
    time.sleep(10)

    status = get_camera_status_with_retry(attempts=3, timeout=2)
    detail = status_detail(status)
    set_device_state(status.state, detail)
    return not status.ota_only


def start_startup(force: bool = False) -> None:
    global startup_thread, camera_adjustments_done
    with startup_lock:
        if force:
            startup_complete.clear()
            set_device_state(
                "wifi-online-camera-starting",
                "running quality/clock startup checks before opening MJPEG",
            )
            # Reset camera adjustments flag so they run again after this startup
            with camera_adjustments_lock:
                camera_adjustments_done = False

            # Note: We don't stop monitoring threads here because they should continue
            # running and showing connection status even during restarts.
            # The key is that startup_complete controls the main loop behavior.

        if startup_complete.is_set():
            return
        if startup_thread is None or not startup_thread.is_alive():
            # Reset camera adjustments flag for new startup
            with camera_adjustments_lock:
                camera_adjustments_done = False

            def _runner() -> None:
                attempt = 1
                while not startup_complete.is_set():
                    try:
                        print(f"Running startup attempt {attempt}...")
                        set_device_state(
                            "wifi-online-camera-starting",
                            f"startup attempt {attempt}: polling status and applying camera settings",
                        )
                        startup()
                        set_device_state(
                            "camera-online",
                            "startup complete; opening MJPEG stream next",
                        )
                        startup_complete.set()
                        print("Startup completed successfully!")
                        # Monitoring threads are already started in main()
                    except CameraRecoveryMode as exc:
                        set_device_state("ota-only", str(exc))
                        print(exc)
                        print("Attempting to exit OTA-only recovery mode.")
                        if exit_ota_recovery_and_check():
                            attempt += 1
                            continue
                        print("Startup paused because device is still in OTA-only recovery mode.")
                        break
                    except Exception as exc:
                        set_device_state(
                            "offline",
                            f"startup attempt {attempt} failed: {exc}",
                        )
                        print(f"Startup failed with error: {exc}")
                        print("Retrying startup in 5 s...")
                        time.sleep(5)
                        attempt += 1

            startup_thread = threading.Thread(target=_runner, daemon=True)
            startup_thread.start()


def apply_camera_adjustments() -> None:
    """Apply camera adjustments after stream has started (runs in background thread)."""

    def _adjust() -> None:
        try:
            # Wait 20 seconds after stream starts
            print("Waiting 20 seconds for stream to stabilize...")
            time.sleep(20)

            # Disable auto white balance
            try:
                print("Disabling auto white balance (awb=0)")
                resp = requests.get(
                    "http://192.168.0.13/control?var=awb&val=0", timeout=2
                )
                if resp.status_code == 200:
                    print("AWB disabled successfully")
            except Exception as e:
                print(f"Setting AWB failed: {e}")

            time.sleep(2)

            current_hour_ist = datetime.now(IST).hour
            if not (12 <= current_hour_ist < 18):
                # Set auto exposure level only outside the afternoon window in IST
                try:
                    print("Setting auto exposure level (ae_level=2)")
                    resp = requests.get(
                        "http://192.168.0.13/control?var=ae_level&val=2", timeout=2
                    )
                    if resp.status_code == 200:
                        print("AE level set successfully")
                except Exception as e:
                    print(f"Setting AE level failed: {e}")
            else:
                print("Skipping AE level change during 12pm-6pm IST window")

            time.sleep(2)

            # Disable auto gain control
            try:
                print("Disabling auto gain control (agc=0)")
                resp = requests.get(
                    "http://192.168.0.13/control?var=agc&val=0", timeout=2
                )
                if resp.status_code == 200:
                    print("AGC disabled successfully")
            except Exception as e:
                print(f"Setting AGC failed: {e}")

            time.sleep(2)
            print("Camera adjustments completed")

        except Exception as e:
            print(f"Camera adjustments error: {e}")

    adj_thread = threading.Thread(target=_adjust, daemon=True)
    adj_thread.start()
    print(
        "Camera adjustment thread started (will apply settings after stream stabilizes)"
    )


def start_rssi_monitor() -> None:
    """Start background thread to monitor RSSI every 10 seconds."""
    global rssi_thread

    def _rssi_monitor() -> None:
        global rssi_value
        while True:
            if not startup_complete.is_set():
                time.sleep(1.0)
                continue
            try:
                new_rssi = get_rssi(timeout=2.0)
                with rssi_lock:
                    rssi_value = new_rssi
            except Exception:
                pass
            time.sleep(rssi_update_interval)

    if rssi_thread is None or not rssi_thread.is_alive():
        rssi_thread = threading.Thread(target=_rssi_monitor, daemon=True)
        rssi_thread.start()


def start_temperature_monitor() -> None:
    """Start background thread to monitor ESP32 SoC temperature every 10 seconds."""
    global temperature_thread

    def _temperature_monitor() -> None:
        global soc_temperature_c
        while True:
            if not startup_complete.is_set():
                time.sleep(1.0)
                continue
            try:
                response = requests.get("http://192.168.0.13/syshealth", timeout=3.0)
                if response.status_code == 200:
                    data = response.json()
                    temp_c = data.get("socTempC")

                    if temp_c is None:
                        raise ValueError("syshealth response missing socTempC")

                    with temperature_lock:
                        soc_temperature_c = float(temp_c)
            except requests.exceptions.Timeout:
                pass
            except requests.exceptions.RequestException:
                pass
            except Exception:
                pass

            time.sleep(temperature_update_interval)

    if temperature_thread is None or not temperature_thread.is_alive():
        temperature_thread = threading.Thread(target=_temperature_monitor, daemon=True)
        temperature_thread.start()


def update_fps() -> None:
    """Update FPS calculation based on frame timestamps."""
    global fps_value, fps_frame_times

    current_time = time.time()

    with fps_lock:
        # Add current frame time
        fps_frame_times.append(current_time)

        # Keep only recent frames (last N frames)
        if len(fps_frame_times) > FPS_SAMPLE_WINDOW:
            fps_frame_times.pop(0)

        # Calculate FPS if we have enough samples
        if len(fps_frame_times) >= 2:
            time_span = fps_frame_times[-1] - fps_frame_times[0]
            if time_span > 0:
                fps_value = (len(fps_frame_times) - 1) / time_span


def draw_box(
    frame,
    x,
    y,
    w,
    h,
    bg_color=(245, 247, 250),
    alpha=0.88,
    border_color=None,
    accent_color=None,
):
    """Draw a square Material dark HUD surface."""
    x2 = x + w
    y2 = y + h

    if x >= frame.shape[1] or y >= frame.shape[0] or x2 <= 0 or y2 <= 0:
        return

    x = max(0, x)
    y = max(0, y)
    x2 = min(frame.shape[1] - 1, x2)
    y2 = min(frame.shape[0] - 1, y2)
    w = x2 - x
    h = y2 - y

    shadow = frame.copy()
    shadow_y = min(frame.shape[0] - 1, y + 2)
    shadow_y2 = min(frame.shape[0] - 1, y2 + 2)
    cv2.rectangle(shadow, (x, shadow_y), (x2, shadow_y2), (0, 0, 0), -1)
    cv2.addWeighted(shadow, 0.18, frame, 0.82, 0, frame)

    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x2, y2), bg_color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    if border_color is not None:
        cv2.rectangle(frame, (x, y), (x2, y2), border_color, 1, cv2.LINE_AA)
    if accent_color is not None:
        cv2.rectangle(frame, (x, y), (x + 3, y2), accent_color, -1)


def draw_wifi_icon(frame, x, y, size, rssi, color):
    """Draw a compact signal-strength bar icon without dots."""
    thickness = 1
    bars = 0
    if rssi is not None:
        if rssi >= -60:
            bars = 4
        elif rssi >= -67:
            bars = 3
        elif rssi >= -75:
            bars = 2
        elif rssi >= -85:
            bars = 1

    grey = (88, 93, 101)
    bar_w = 3
    gap = 2
    base_y = y + size - 3
    for i in range(4):
        bar_h = 4 + i * 4
        x1 = x + i * (bar_w + gap)
        y1 = base_y - bar_h
        curr_color = color if i < bars else grey
        cv2.rectangle(frame, (x1, y1), (x1 + bar_w, base_y), curr_color, -1)
        cv2.rectangle(frame, (x1, y1), (x1 + bar_w, base_y), curr_color, thickness)

    return size


def get_status_color(value, thresholds, colors):
    """Returns color based on value and thresholds (descending quality)."""
    if value is None:
        return (128, 128, 128)
    for limit, color in zip(thresholds, colors):
        if value >= limit:
            return color
    return colors[-1]


HUD_FONT_CACHE: dict[int, Optional[object]] = {}


def get_hud_font(size: int) -> Optional[object]:
    if ImageFont is None:
        return None
    if size not in HUD_FONT_CACHE:
        try:
            HUD_FONT_CACHE[size] = ImageFont.truetype(HUD_FONT_PATH, size)
        except OSError:
            HUD_FONT_CACHE[size] = None
    return HUD_FONT_CACHE[size]


def get_hud_text_size(text: str, font_size: int, fallback_font, fallback_scale: float, thickness: int) -> tuple[int, int]:
    hud_font = get_hud_font(font_size)
    if hud_font is None:
        (tw, th), _ = cv2.getTextSize(text, fallback_font, fallback_scale, thickness)
        return tw, th

    bbox = hud_font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def put_hud_text(
    frame: np.ndarray,
    text: str,
    x: int,
    center_y: int,
    color: tuple[int, int, int],
    font_size: int,
    fallback_font,
    fallback_scale: float,
    thickness: int,
) -> None:
    hud_font = get_hud_font(font_size)
    if hud_font is None or Image is None or ImageDraw is None:
        (_, th), _ = cv2.getTextSize(text, fallback_font, fallback_scale, thickness)
        cv2.putText(
            frame,
            text,
            (x, center_y + th // 2),
            fallback_font,
            fallback_scale,
            color,
            thickness,
            cv2.LINE_AA,
        )
        return

    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb_frame)
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), text, font=hud_font)
    text_h = bbox[3] - bbox[1]
    y = int(center_y - text_h / 2 - bbox[1])
    draw.text((x, y), text, font=hud_font, fill=(color[2], color[1], color[0]))
    frame[:] = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


def draw_hud(
    frame: np.ndarray,
    fps: float,
    rssi: int | None,
    soc_temp_c: float | None,
    motion_detected: bool = False,
    show_time: bool = True,
    coordinates: list = [0, 0],
):
    """Draws a square Material dark HUD."""
    x, y = coordinates
    _, w = frame.shape[:2]

    top_margin = 14
    box_h = 34
    pad_x = 12
    gap = 6

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_size = 14
    font_scale = 0.44
    font_color = (232, 234, 237)
    muted_color = (154, 160, 166)
    thickness = 1
    panel_color = (31, 31, 31)
    panel_border = (70, 70, 70)
    surface_variant = (38, 38, 38)
    status_good = (129, 201, 149)
    status_warn = (251, 188, 5)
    status_bad = (242, 139, 130)
    status_neutral = (189, 193, 198)
    motion_panel = (37, 37, 37)
    motion_accent = status_warn

    ts_label = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    tw, th = get_hud_text_size(ts_label, font_size, font, font_scale, thickness)
    ts_box_w = tw + (pad_x * 2)

    text_center_y = top_margin + box_h // 2
    overlap_pad = 4

    def overlaps_box(box_x: int, box_y: int, box_w: int, box_h: int) -> bool:
        return (
            box_x - overlap_pad <= x <= box_x + box_w + overlap_pad
            and box_y - overlap_pad <= y <= box_y + box_h + overlap_pad
        )

    now = time.monotonic()

    def should_draw(key: str, box_x: int, box_y: int, box_w: int, box_h: int) -> bool:
        if overlaps_box(box_x, box_y, box_w, box_h):
            HUD_COOLDOWN.set_hidden(key, now, HUD_HIDE_SECONDS)
            return False
        return not HUD_COOLDOWN.is_hidden(key, now)

    if should_draw("timestamp", gap, top_margin, ts_box_w, box_h):
        draw_box(
            frame,
            gap,
            top_margin,
            ts_box_w,
            box_h,
            bg_color=panel_color,
            border_color=panel_border,
        )
        put_hud_text(
            frame,
            ts_label,
            gap + pad_x,
            text_center_y,
            font_color,
            font_size,
            font,
            font_scale,
            thickness,
        )

    if motion_detected:
        warn_text = "MOTION"
        tw, th = get_hud_text_size(warn_text, font_size, font, font_scale, thickness)
        warn_box_w = tw + (pad_x * 2)
        warn_x = gap + ts_box_w + gap
        if should_draw("motion_warn", warn_x, top_margin, warn_box_w, box_h):
            draw_box(
                frame,
                warn_x,
                top_margin,
                warn_box_w,
                box_h,
                bg_color=motion_panel,
                alpha=0.94,
                border_color=(92, 76, 31),
                accent_color=motion_accent,
            )
            put_hud_text(
                frame,
                warn_text,
                warn_x + pad_x + 4,
                text_center_y,
                font_color,
                font_size,
                font,
                font_scale,
                thickness,
            )

    cursor_x = w - gap

    wifi_text = f"{rssi}dBm" if rssi is not None else "--dBm"
    tw, th = get_hud_text_size(wifi_text, font_size, font, font_scale, thickness)

    icon_size = 18
    icon_pad = 8
    wifi_box_w = tw + icon_size + icon_pad + (pad_x * 2) + 2

    cursor_x -= wifi_box_w
    if should_draw("wifi", cursor_x, top_margin, wifi_box_w, box_h):
        wifi_color = get_status_color(
            rssi,
            [-60, -70, -80],
            [status_good, status_neutral, status_warn, status_bad],
        )
        draw_box(
            frame,
            cursor_x,
            top_margin,
            wifi_box_w,
            box_h,
            bg_color=surface_variant,
            border_color=panel_border,
            accent_color=None,
        )

        icon_x = cursor_x + pad_x
        draw_wifi_icon(frame, icon_x, top_margin + 7, icon_size, rssi, wifi_color)

        put_hud_text(
            frame,
            wifi_text,
            icon_x + icon_size + icon_pad,
            text_center_y,
            font_color if rssi is not None else muted_color,
            font_size,
            font,
            font_scale,
            thickness,
        )

    cursor_x -= gap

    fps_val = int(fps)
    fps_str = f"{fps_val} fps"
    tw, th = get_hud_text_size(fps_str, font_size, font, font_scale, thickness)

    fps_box_w = tw + (pad_x * 2) + 18
    cursor_x -= fps_box_w
    if should_draw("fps", cursor_x, top_margin, fps_box_w, box_h):
        fps_color = get_status_color(
            fps, [7, 5], [status_good, status_warn, status_bad]
        )
        draw_box(
            frame,
            cursor_x,
            top_margin,
            fps_box_w,
            box_h,
            bg_color=surface_variant,
            border_color=panel_border,
            accent_color=fps_color,
        )

        put_hud_text(
            frame,
            fps_str,
            cursor_x + pad_x + 4,
            text_center_y,
            font_color,
            font_size,
            font,
            font_scale,
            thickness,
        )

    cursor_x -= gap

    if SHOW_TEMPERATURE_BADGE:
        temp_val = f"{soc_temp_c:.1f}C" if soc_temp_c is not None else "--C"
        tw, th = get_hud_text_size(temp_val, font_size, font, font_scale, thickness)

        icon_w = 13
        icon_pad = 6
        temp_box_w = tw + icon_w + icon_pad + (pad_x * 2) + 2

        cursor_x -= temp_box_w
        if should_draw("temperature", cursor_x, top_margin, temp_box_w, box_h):
            if soc_temp_c is None:
                temp_color = (128, 128, 128)
            elif soc_temp_c >= 80:
                temp_color = status_bad
            elif soc_temp_c >= 70:
                temp_color = status_warn
            else:
                temp_color = status_good
            draw_box(
                frame,
                cursor_x,
                top_margin,
                temp_box_w,
                box_h,
                bg_color=surface_variant,
                border_color=panel_border,
                accent_color=None,
            )

            ic_x = cursor_x + pad_x
            ic_y = top_margin + 7
            stem_x = ic_x + icon_w // 2
            stem_top = ic_y + 2
            stem_bottom = ic_y + 14
            bulb_center = (stem_x, ic_y + 18)

            cv2.line(
                frame,
                (stem_x, stem_top),
                (stem_x, stem_bottom),
                temp_color,
                3,
                cv2.LINE_AA,
            )
            cv2.circle(frame, (stem_x, stem_top), 2, temp_color, 1, cv2.LINE_AA)
            cv2.circle(frame, bulb_center, 5, temp_color, -1, cv2.LINE_AA)
            cv2.circle(frame, bulb_center, 5, temp_color, 1, cv2.LINE_AA)

            tick_color = font_color if soc_temp_c is not None else muted_color
            cv2.line(
                frame,
                (stem_x + 4, ic_y + 5),
                (stem_x + 7, ic_y + 5),
                tick_color,
                1,
                cv2.LINE_AA,
            )
            cv2.line(
                frame,
                (stem_x + 4, ic_y + 10),
                (stem_x + 7, ic_y + 10),
                tick_color,
                1,
                cv2.LINE_AA,
            )

            put_hud_text(
                frame,
                temp_val,
                ic_x + icon_w + icon_pad,
                text_center_y,
                font_color if soc_temp_c is not None else muted_color,
                font_size,
                font,
                font_scale,
                thickness,
            )

        cursor_x -= gap


def backoff(attempt: int) -> float:
    return min(5.0, 2 ** max(0, attempt - 1))


def draw_status_message(frame: np.ndarray, message: str) -> None:
    for index, line in enumerate(message.splitlines()):
        cv2.putText(
            frame,
            line[:82],
            (30, 100 + index * 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (220, 220, 220),
            1,
            cv2.LINE_AA,
        )


def show_placeholder(message: str) -> None:
    if not SHOW_LOCAL_VIEW:
        return  # Don't show placeholder if local view is disabled
    base = (
        no_signal_img
        if no_signal_img is not None
        else np.zeros((480, 640, 3), dtype=np.uint8)
    )
    frame = base.copy()

    draw_status_message(frame, message)

    # Use draw_hud with placeholders
    draw_hud(frame, fps=0, rssi=None, soc_temp_c=None)

    cv2.imshow("frame", frame)


def show_no_signal_frame(message: str) -> Optional[np.ndarray]:
    """Create and optionally display a no-signal frame. Always returns the frame for recording."""
    # Initialize frame from no_signal_img
    if no_signal_img is not None:
        frame = no_signal_img.copy()
    else:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            "NO SIGNAL",
            (160, 260),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            (0, 0, 255),
            3,
            cv2.LINE_AA,
        )

    draw_status_message(frame, message)

    # Get current status values
    with rssi_lock:
        current_rssi = rssi_value
    with fps_lock:
        current_fps = fps_value
    with temperature_lock:
        current_temperature = soc_temperature_c

    # Draw HUD
    draw_hud(frame, current_fps, current_rssi, current_temperature)

    # Show in window if enabled
    if SHOW_LOCAL_VIEW:
        cv2.imshow("frame", frame)

    return frame


def get_no_signal_frame_for_size(width: int, height: int, message: str) -> np.ndarray:
    """Create a no-signal frame matching the specified dimensions for FFmpeg."""
    # Create or resize no_signal base to match camera dimensions
    if no_signal_img is not None:
        base = cv2.resize(no_signal_img, (width, height))
    else:
        base = np.zeros((height, width, 3), dtype=np.uint8)
        cv2.putText(
            base,
            "NO SIGNAL",
            (width // 4, height // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            (0, 0, 255),
            3,
            cv2.LINE_AA,
        )

    frame = base.copy()

    draw_status_message(frame, message)

    # Get current status values
    with rssi_lock:
        current_rssi = rssi_value
    with fps_lock:
        current_fps = fps_value
    with temperature_lock:
        current_temperature = soc_temperature_c

    # Draw HUD
    draw_hud(frame, current_fps, current_rssi, current_temperature)

    return frame


def record_no_signal_frame(message: str) -> None:
    """Show (if requested) and record a no-signal frame sized for the encoder."""
    display_frame = show_no_signal_frame(message)

    if not ENABLE_RECORDING:
        return

    if expected_frame_size:
        frame_for_record = get_no_signal_frame_for_size(
            expected_frame_size[0], expected_frame_size[1], message
        )
    else:
        frame_for_record = display_frame

    if frame_for_record is not None:
        write_frame_to_ffmpeg(frame_for_record)


def open_capture_with_timeout() -> Optional[MjpegStreamCapture]:
    """Open the HTTP MJPEG stream with a bounded connection timeout."""
    status = get_camera_status_with_retry(attempts=3, timeout=2)
    detail = status_detail(status)
    set_device_state(status.state, detail)

    if status.ota_only:
        reason = f": {status.reason}" if status.reason else ""
        print(f"Device is in recovery / OTA-only mode{reason}")
        return None
    if not status.camera_online:
        print("Device is offline or camera is still starting; stream open deferred.")
        return None

    cap = MjpegStreamCapture(
        MJPEG_STREAM_URL,
        open_timeout=CAPTURE_OPEN_TIMEOUT,
        read_timeout=FRAME_READ_TIMEOUT,
    )
    if cap.open():
        set_device_state("camera-online", f"{detail}; MJPEG stream opened")
        return cap
    set_device_state(status.state, f"{detail}; MJPEG stream open failed")
    cap.release()
    return None


class FrameReadTimeout(Exception):
    pass


def _timeout_handler(_signum, _frame):
    raise FrameReadTimeout()


def read_frame_with_timeout(cap: MjpegStreamCapture):
    """Read one frame while making SIGALRM cleanup part of the recovery path."""
    ret = False
    frame = None
    timed_out = False

    try:
        signal.setitimer(signal.ITIMER_REAL, FRAME_READ_TIMEOUT)
        ret, frame = cap.read()
    except FrameReadTimeout:
        timed_out = True
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        except FrameReadTimeout:
            timed_out = True
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            except FrameReadTimeout:
                pass

    if timed_out:
        print("Frame read timed out - forcing restart.")
        return False, None
    return ret, frame


def main() -> None:
    global ffmpeg_record_proc, ffmpeg_rtsp_proc, expected_frame_size, current_fps, camera_adjustments_done
    attempt = 0
    cap = None
    last_blinker_trigger = 0.0

    # Initialize motion detection components
    mog2 = cv2.createBackgroundSubtractorMOG2(
        history=500, varThreshold=25, detectShadows=True
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    blinker = NonBlockingBlinker(blink_interval=0.5)

    # Ensure SIGALRM interrupts blocking frame reads after FRAME_READ_TIMEOUT seconds.
    signal.signal(signal.SIGALRM, _timeout_handler)

    print("Starting camera initialization in background...")
    if ENABLE_RECORDING:
        print(f"Recording enabled: {BASE_DIR}")
        if USE_DYNAMIC_FPS:
            print(
                f"Segment duration: {SEGMENT_SECONDS}s, FPS: Dynamic (matches source)"
            )
        else:
            print(
                f"Segment duration: {SEGMENT_SECONDS}s, FPS: {FIXED_OUTPUT_FPS:.0f} (fixed)"
            )
    if not SHOW_LOCAL_VIEW:
        print("Local view disabled - running in headless mode")
        print("Press Ctrl+C to stop")
    start_startup(force=True)
    # Start monitoring threads early so they show status during startup
    start_rssi_monitor()
    if SHOW_TEMPERATURE_BADGE:
        start_temperature_monitor()
    show_placeholder(get_device_state_message("STARTUP"))
    cv2.waitKey(1)

    try:
        while True:
            # Only check for 'q' key if showing local view
            if SHOW_LOCAL_VIEW:
                if cv2.waitKey(1) == ord("q"):
                    break
            else:
                # Small sleep to prevent tight loop when not showing view
                time.sleep(0.01)

            if not startup_complete.is_set():
                if cap is not None:
                    cap.release()
                    cap = None

                record_no_signal_frame(get_device_state_message("STARTUP"))

                time.sleep(0.05)
                continue

            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                    time.sleep(FRAME_FAILURE_RECONNECT_DELAY)
                    cap = None

                record_no_signal_frame(
                    get_device_state_message("STREAM", attempt=attempt + 1)
                )

                cap = open_capture_with_timeout()
                if cap is None or not cap.isOpened():
                    print(f"Failed to open stream on attempt {attempt + 1}")
                    if cap is not None:
                        cap.release()
                    cap = None
                    attempt += 1
                    time.sleep(backoff(attempt))
                    if not is_device_ota_only():
                        start_startup(force=True)
                    continue
                print("Connection established.")
                set_device_state("camera-online", "MJPEG stream connected")
                attempt = 0

            ret, frame = read_frame_with_timeout(cap)

            if not ret or frame is None:
                print("Frame read failed - signal lost.")
                cap.release()
                cap = None
                start_startup(force=True)

                # Show and record "no signal" frame
                # Camera crashed during operation - restarting everything
                record_no_signal_frame("CRASH: Restarting camera...")

                time.sleep(FRAME_FAILURE_RECONNECT_DELAY)
                continue

            # Apply camera adjustments after first successful frame (only once per startup)
            with camera_adjustments_lock:
                if not camera_adjustments_done:
                    camera_adjustments_done = True
                    apply_camera_adjustments()

            # Update FPS calculation
            update_fps()

            # Motion detection on the current frame
            fg_mask = mog2.apply(frame)
            _, mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            mask = cv2.dilate(mask, kernel, iterations=2)

            # Build ROI mask and apply
            roi_mask = np.zeros_like(mask, dtype=np.uint8)
            cv2.fillPoly(roi_mask, [ROI_PTS], 255)
            filtered_motion = cv2.bitwise_and(mask, roi_mask)

            # Find contours in filtered motion
            contours, _ = cv2.findContours(
                filtered_motion, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            disp = frame.copy()
            motion_detected = False
            time_overlap = False
            coordinates = [0, 0]
            for c in contours:
                area = cv2.contourArea(c)
                if area < MIN_AREA:
                    continue
                motion_detected = True
                x, y, w, h = cv2.boundingRect(c)
                coordinates = [x, y]
                if 10 <= x <= 46 and 15 <= y <= 276:
                    time_overlap = True

                # Only draw motion boxes if flag is enabled
                if SHOW_MOTION_BOXES:
                    cv2.rectangle(disp, (x, y), (x + w, y + h), (0, 255, 255), 2)
                    cx, cy = x + w // 2, y + h // 2
                    cv2.circle(disp, (cx, cy), 3, (0, 255, 255), -1)
                    cv2.putText(
                        disp,
                        f"motion {area:.0f}",
                        (x, max(0, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )

            # Drive non-blocking blinker on motion (debounced to avoid
            # hammering the ESP32 with LED HTTP requests)
            if motion_detected:
                now_mono = time.monotonic()
                if not blinker.is_active and (now_mono - last_blinker_trigger) >= BLINKER_COOLDOWN:
                    blinker.start(duration=1)
                    last_blinker_trigger = now_mono
                # Trigger accumulated motion event tracking
                acc.trigger()

            # Update blinker, but catch errors if camera is not responding
            try:
                blinker.update()
            except Exception as e:
                print(f"Warning: Blinker update failed: {e}")

            # Draw HUD (Timestamp, Status Badges, Motion Warning)
            with rssi_lock:
                current_rssi = rssi_value
            with fps_lock:
                current_fps = fps_value
            with temperature_lock:
                current_temperature = soc_temperature_c

            draw_hud(
                disp,
                current_fps,
                current_rssi,
                current_temperature,
                motion_detected,
                time_overlap,
                coordinates,
            )

            # Draw ROI polygon on display only if flag is enabled
            if SHOW_MOTION_BOXES:
                cv2.polylines(
                    disp,
                    [ROI_PTS],
                    isClosed=True,
                    color=(0, 255, 255),
                    thickness=1,
                    lineType=cv2.LINE_AA,
                )

            # Record frame with overlay (IN-PLACE recording with motion detection)
            if ENABLE_RECORDING:
                write_frame_to_ffmpeg(disp)

            # Display only if flag is enabled
            if SHOW_LOCAL_VIEW:
                cv2.imshow("frame", disp)
                cv2.imshow("ROI mask", roi_mask)

    finally:
        # Cleanup
        print("\nShutting down...")
        if cap is not None:
            cap.release()
        with ffmpeg_lock:
            if ffmpeg_record_proc is not None:
                stop_ffmpeg(ffmpeg_record_proc)
                ffmpeg_record_proc = None
            if ffmpeg_rtsp_proc is not None:
                stop_ffmpeg(ffmpeg_rtsp_proc)
                ffmpeg_rtsp_proc = None
            expected_frame_size = None
        cv2.destroyAllWindows()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
