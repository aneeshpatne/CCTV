import os
import sys

# CRITICAL: Set FFMPEG options BEFORE importing cv2
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "protocol_whitelist;file,http,https,tcp|"
    "analyzeduration;0|"
    "probesize;32|"          # tiny probe
    "fflags;nobuffer|"       # minimize internal buffering
    "flags;low_delay|"       # lower latency
    "max_delay;0|"           # no queuing delay
)

import threading
import time
import signal
import subprocess
import requests
from datetime import datetime
from typing import Optional
from pathlib import Path

import cv2
import numpy as np
import pytz
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utilities.startup import startup
from utilities.warn import NonBlockingBlinker
from tools.get_rssi import get_rssi
from utilities.motion_db import log_motion_event

URL = "http://192.168.0.13:81/stream"
IST = pytz.timezone('Asia/Kolkata')
NO_SIGNAL_PATH = os.path.join(os.path.dirname(__file__), 'examples', 'no_signal.png')
FRAME_RETRY_DELAY = 0.5
FRAME_READ_TIMEOUT = 5.0  # seconds
CAPTURE_OPEN_TIMEOUT = 10.0  # seconds to wait for capture to open

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
USE_DYNAMIC_FPS = True  # Match source FPS dynamically instead of enforcing fixed rate
VIDEO_BITRATE_KBPS = int(os.getenv("CCTV_VIDEO_BITRATE_KBPS", "1500"))
VIDEO_BUFSIZE_KBPS = int(os.getenv("CCTV_VIDEO_BUFSIZE_KBPS", str(VIDEO_BITRATE_KBPS * 2)))

# Display configuration
SHOW_MOTION_BOXES = False  # Show motion detection boxes and ROI polygon
SHOW_LOCAL_VIEW = False    # Show CV2 preview windows
SHOW_MEMORY_BADGE = True   # Show ESP32 memory usage badge

# Motion detection configuration
MIN_AREA = 800
ROI_PTS = np.array([
    [12, 5], [34, 4], [69, 1], [94, 3], [122, 10], [137, 3],
    [161, 21], [178, 55], [188, 74], [218, 64], [242, 60], [260, 59],
    [299, 58], [340, 66], [393, 71], [432, 74], [461, 72], [489, 67],
    [515, 63], [561, 66], [617, 88], [660, 91], [732, 90], [765, 76],
    [780, 71], [815, 58], [818, 35], [814, 16], [845, 7], [873, 10],
    [920, 9], [949, 14], [985, 14], [1009, 13], [1020, 43], [1021, 71],
    [1018, 98], [1023, 130], [1023, 154], [1016, 194], [1021, 241], [1023, 323],
    [1023, 333], [1018, 354], [1020, 502], [1020, 559], [1017, 606], [1023, 676],
    [1016, 720], [1015, 756], [967, 761], [923, 758], [873, 761], [805, 765],
    [730, 752], [687, 757], [570, 754], [478, 755], [424, 750], [354, 749],
    [282, 755], [219, 757], [129, 752], [87, 753], [46, 746], [14, 742],
    [9, 697], [11, 641], [12, 598], [11, 553], [12, 506], [12, 441],
    [9, 377], [13, 319], [13, 259], [6, 199], [13, 117], [11, 61],
    [10, 8],
], dtype=np.int32)

no_signal_img = cv2.imread(NO_SIGNAL_PATH)
if no_signal_img is None:
    print(f"Warning: Could not load no_signal.png from {NO_SIGNAL_PATH}")
    no_signal_img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(no_signal_img, "NO SIGNAL", (160, 260), cv2.FONT_HERSHEY_SIMPLEX,
                1.4, (0, 0, 255), 3, cv2.LINE_AA)

startup_complete = threading.Event()
startup_thread = None
startup_lock = threading.Lock()

# Camera adjustment state (run after stream starts)
camera_adjustments_done = False
camera_adjustments_lock = threading.Lock()

# Capture opening state
capture_result = {'cap': None, 'done': False}
capture_lock = threading.Lock()

# RSSI monitoring state
rssi_value = None
rssi_lock = threading.Lock()
rssi_thread = None
rssi_update_interval = 10  # Update RSSI every 10 seconds

# Memory monitoring state
memory_percent = None
memory_lock = threading.Lock()
memory_thread = None
memory_update_interval = 10  # Update memory every 10 seconds

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

# Motion detection logging state
motion_log_queue = []
motion_log_lock = threading.Lock()
motion_log_thread = None
motion_debounce_seconds = 60  # Log motion at most once per minute
last_motion_log_time = 0.0

# Recording state
ffmpeg_record_proc: Optional[subprocess.Popen] = None
ffmpeg_rtsp_proc: Optional[subprocess.Popen] = None
ffmpeg_lock = threading.Lock()
expected_frame_size: Optional[tuple[int, int]] = None  # (width, height) that FFmpeg expects
current_fps: Optional[float] = None  # Dynamically calculated FPS for FFmpeg


def start_ffmpeg_record(width: int, height: int, fps: float) -> Optional[subprocess.Popen]:
    """Start FFmpeg process for variable frame rate CCTV recording."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    out_pattern = BASE_DIR / "recording_%Y%m%d_%H%M%S.mp4"
    safe_fps = max(1.0, fps)
    gop_size = max(1, int(round(safe_fps * 2)))

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",

        # raw frames over stdin
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", f"{safe_fps:.2f}",                 # Match input cadence to measured FPS
        "-use_wallclock_as_timestamps", "1",
        "-i", "-",

        "-map", "0:v",

        # videotoolbox-friendly pixel format
        "-vf", "format=nv12",

        # hardware encoder (Intel Mac)
        "-c:v", "h264_videotoolbox",

        # stable quality (avoid blur/clear cycling)
        "-b:v", f"{VIDEO_BITRATE_KBPS}k",
        "-maxrate", f"{VIDEO_BITRATE_KBPS}k",
        "-bufsize", f"{VIDEO_BUFSIZE_KBPS}k",

        # GOP: ~2 seconds based on current FPS
        "-g", str(gop_size),
        "-bf", "0",

        # segmenting
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        "-reset_timestamps", "1",
        "-strftime", "1",
        out_pattern,
    ]


    # ---- Spawn process with logging -------------------------------------------
    try:
        log_path = BASE_DIR / "ffmpeg_record.log"
        logf = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=logf,          # keep stderr for diagnostics
            bufsize=0
        )
        print(f"FFmpeg VFR recording started: {out_pattern}")
        return proc
    except Exception as e:
        print(f"Failed to start FFmpeg: {e}")
        return None


def start_ffmpeg_rtsp(width: int, height: int, fps: float) -> Optional[subprocess.Popen]:
    """Start FFmpeg process for variable frame rate RTSP restream."""
    safe_fps = max(1.0, fps)
    gop_size = max(1, int(round(safe_fps * 2)))
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",

        # Raw frames from Python
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", f"{safe_fps:.2f}",                 # Match input cadence to measured FPS
        "-use_wallclock_as_timestamps", "1",
        "-i", "-",

        "-map", "0:v",

        # Convert to videotoolbox-friendly format
        "-vf", "format=nv12",

        # Hardware encoder (Intel Quick Sync via VideoToolbox)
        "-c:v", "h264_videotoolbox",

        # Stable bitrate (no pulsing)
        "-b:v", f"{VIDEO_BITRATE_KBPS}k",
        "-maxrate", f"{VIDEO_BITRATE_KBPS}k",
        "-bufsize", f"{VIDEO_BUFSIZE_KBPS}k",

        # GOP / latency
        "-g", str(gop_size),                     # ~2 seconds of frames
        "-bf", "0",

        # RTSP output
        "-rtsp_transport", "tcp",
        "-f", "rtsp",
        RTSP_OUT,
    ]



    try:
        log_path = BASE_DIR / "ffmpeg_rtsp.log"
        logf = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=logf,
            bufsize=0
        )
        print(f"FFmpeg VFR RTSP started: {RTSP_OUT}")
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
    global ffmpeg_record_proc, ffmpeg_rtsp_proc, expected_frame_size, current_fps

    if not ENABLE_RECORDING and not ENABLE_RTSP:
        return True

    with ffmpeg_lock:
        h, w = frame.shape[:2]
        new_size = (w, h)

        # Get current FPS from the FPS tracker
        with fps_lock:
            measured_fps = fps_value if fps_value > 0 else 10.0  # fallback to 10 if not calculated yet

        # Check if we need to restart FFmpeg due to size or FPS change
        fps_changed = False
        if USE_DYNAMIC_FPS and current_fps is not None:
            # Restart if FPS changes by more than 1 FPS to avoid constant restarts from small fluctuations
            if abs(measured_fps - current_fps) > 1.0:
                fps_changed = True
                print(f"FPS changed from {current_fps:.2f} to {measured_fps:.2f}; restarting FFmpeg pipelines.")

        # Track the canonical size expected by the encoders
        if expected_frame_size is None:
            expected_frame_size = new_size
            current_fps = measured_fps
        elif new_size != expected_frame_size or fps_changed:
            if new_size != expected_frame_size:
                print(
                    f"Frame size changed from {expected_frame_size[0]}x{expected_frame_size[1]} to {w}x{h}; "
                    "restarting FFmpeg pipelines."
                )
            if ffmpeg_record_proc is not None:
                stop_ffmpeg(ffmpeg_record_proc)
                ffmpeg_record_proc = None
            if ffmpeg_rtsp_proc is not None:
                stop_ffmpeg(ffmpeg_rtsp_proc)
                ffmpeg_rtsp_proc = None
            expected_frame_size = new_size
            current_fps = measured_fps

        target_width, target_height = expected_frame_size
        target_fps = current_fps if current_fps is not None else measured_fps

        # Ensure recording process is alive when recording enabled
        if ENABLE_RECORDING:
            if ffmpeg_record_proc is not None and ffmpeg_record_proc.poll() is not None:
                exit_code = ffmpeg_record_proc.poll()
                print(f"Recording FFmpeg exited (code {exit_code}); restarting...")
                stop_ffmpeg(ffmpeg_record_proc)
                ffmpeg_record_proc = None
            if ffmpeg_record_proc is None:
                ffmpeg_record_proc = start_ffmpeg_record(target_width, target_height, target_fps)

        # Ensure RTSP process is alive when enabled
        if ENABLE_RTSP:
            if ffmpeg_rtsp_proc is not None and ffmpeg_rtsp_proc.poll() is not None:
                exit_code = ffmpeg_rtsp_proc.poll()
                print(f"RTSP FFmpeg exited (code {exit_code}); restarting...")
                stop_ffmpeg(ffmpeg_rtsp_proc)
                ffmpeg_rtsp_proc = None
            if ffmpeg_rtsp_proc is None:
                ffmpeg_rtsp_proc = start_ffmpeg_rtsp(target_width, target_height, target_fps)

        # If the current frame size differs from the expected size, resize once for both outputs
        if (w, h) != expected_frame_size:
            frame = cv2.resize(frame, expected_frame_size)

        frame_bytes = frame.tobytes()

        def _write(proc: Optional[subprocess.Popen], label: str,
                   starter) -> Optional[subprocess.Popen]:
            if proc is None:
                return None
            try:
                if proc.stdin:
                    proc.stdin.write(frame_bytes)
            except (BrokenPipeError, IOError) as err:
                print(f"FFmpeg {label} pipe error ({err}); restarting...")
                stop_ffmpeg(proc)
                return starter(target_width, target_height, target_fps)
            return proc

        if ENABLE_RECORDING:
            ffmpeg_record_proc = _write(ffmpeg_record_proc, "recording", start_ffmpeg_record)
        if ENABLE_RTSP:
            ffmpeg_rtsp_proc = _write(ffmpeg_rtsp_proc, "rtsp", start_ffmpeg_rtsp)

        return True


def start_startup(force: bool = False) -> None:
    global startup_thread, camera_adjustments_done
    with startup_lock:
        if force:
            startup_complete.clear()
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
                        startup()
                        startup_complete.set()
                        print("Startup completed successfully!")
                        # Monitoring threads are already started in main()
                    except Exception as exc:
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
                resp = requests.get("http://192.168.0.13/control?var=awb&val=0", timeout=2)
                if resp.status_code == 200:
                    print("AWB disabled successfully")
            except Exception as e:
                print(f"Setting AWB failed: {e}")
            
            time.sleep(2)
            
            # Set auto exposure level
            try:
                print("Setting auto exposure level (ae_level=2)")
                resp = requests.get("http://192.168.0.13/control?var=ae_level&val=2", timeout=2)
                if resp.status_code == 200:
                    print("AE level set successfully")
            except Exception as e:
                print(f"Setting AE level failed: {e}")
            
            time.sleep(2)
            
            # Disable auto gain control
            try:
                print("Disabling auto gain control (agc=0)")
                resp = requests.get("http://192.168.0.13/control?var=agc&val=0", timeout=2)
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
    print("Camera adjustment thread started (will apply settings after stream stabilizes)")


def start_rssi_monitor() -> None:
    """Start background thread to monitor RSSI every 10 seconds."""
    global rssi_thread
    
    def _rssi_monitor() -> None:
        global rssi_value
        while True:
            try:
                new_rssi = get_rssi(timeout=2.0)
                with rssi_lock:
                    rssi_value = new_rssi
                if new_rssi is not None:
                    print(f"RSSI updated: {new_rssi} dBm")
            except Exception as e:
                print(f"RSSI monitoring error: {e}")
            time.sleep(rssi_update_interval)
    
    if rssi_thread is None or not rssi_thread.is_alive():
        rssi_thread = threading.Thread(target=_rssi_monitor, daemon=True)
        rssi_thread.start()
        print(f"RSSI monitor started (updates every {rssi_update_interval}s)")


def start_memory_monitor() -> None:
    """Start background thread to monitor ESP32 memory every 10 seconds."""
    global memory_thread
    
    def _memory_monitor() -> None:
        global memory_percent
        while True:
            try:
                response = requests.get("http://192.168.0.13/syshealth", timeout=3.0)
                if response.status_code == 200:
                    data = response.json()
                    free_heap = data.get('freeHeap', 0)
                    total_heap = data.get('totalHeap', 1)
                    
                    # Calculate percentage of free memory
                    mem_pct = (free_heap / total_heap) * 100 if total_heap > 0 else 0
                    
                    with memory_lock:
                        memory_percent = mem_pct
                    
                    print(f"Memory updated: {mem_pct:.1f}% free ({free_heap}/{total_heap} bytes)")
            except requests.exceptions.Timeout:
                print("Memory monitoring: request timeout")
            except requests.exceptions.RequestException as e:
                print(f"Memory monitoring error: {e}")
            except Exception as e:
                print(f"Memory monitoring unexpected error: {e}")
            
            time.sleep(memory_update_interval)
    
    if memory_thread is None or not memory_thread.is_alive():
        memory_thread = threading.Thread(target=_memory_monitor, daemon=True)
        memory_thread.start()
        print(f"Memory monitor started (updates every {memory_update_interval}s)")


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


def start_motion_logger() -> None:
    """Start background thread to log motion events to database."""
    global motion_log_thread
    
    def _motion_logger() -> None:
        while True:
            try:
                # Check if there are any motion events to log
                with motion_log_lock:
                    if motion_log_queue:
                        timestamp = motion_log_queue.pop(0)
                        try:
                            log_motion_event(timestamp)
                            print(f"Motion logged to database: {timestamp.strftime('%Y-%m-%d %I:%M:%S %p')}")
                        except Exception as e:
                            print(f"Failed to log motion to database: {e}")
                
                time.sleep(1)  # Check queue every second
            except Exception as e:
                print(f"Motion logger error: {e}")
                time.sleep(5)
    
    if motion_log_thread is None or not motion_log_thread.is_alive():
        motion_log_thread = threading.Thread(target=_motion_logger, daemon=True)
        motion_log_thread.start()
        print("Motion logger thread started")


def queue_motion_log(timestamp: datetime) -> None:
    """Queue a motion event for logging with debounce.
    
    Args:
        timestamp: Timestamp of the motion event
    """
    global last_motion_log_time
    
    current_time = time.time()
    
    # Debounce: only log if at least motion_debounce_seconds have passed
    if current_time - last_motion_log_time >= motion_debounce_seconds:
        with motion_log_lock:
            motion_log_queue.append(timestamp)
        last_motion_log_time = current_time


def draw_box(frame, x, y, w, h, bg_color=(10, 10, 10), alpha=0.85):
    """Draws a semi-transparent background box."""
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x + w, y + h), bg_color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

def draw_wifi_icon(frame, x, y, size, rssi, color):
    """Draws a WiFi signal icon using arcs."""
    # center is bottom-middle of the icon area
    cx, cy = x + size // 2, y + size - 4
    radius_step = size // 3
    thickness = 2
    
    # Dot
    cv2.circle(frame, (cx, cy), 2, color, -1)
    
    # Arcs
    # Logic: > -60: 3 arcs, > -70: 2 arcs, > -80: 1 arc
    bars = 0
    if rssi is not None:
        if rssi >= -60: bars = 3
        elif rssi >= -70: bars = 2
        elif rssi >= -80: bars = 1
    
    # Draw background (dim) arcs
    grey = (60, 60, 60)
    
    for i in range(1, 4):
        r = i * radius_step
        curr_color = color if i <= bars else grey
        # StartAngle 225, EndAngle 315 for a top-up wedge look
        cv2.ellipse(frame, (cx, cy), (r, r), 0, 225, 315, curr_color, thickness, cv2.LINE_AA)
    
    return size


def get_status_color(value, thresholds, colors):
    """Returns color based on value and thresholds (descending quality)."""
    if value is None:
        return (128, 128, 128)
    for limit, color in zip(thresholds, colors):
        if value >= limit:
            return color
    return colors[-1]


def draw_hud(frame: np.ndarray, fps: float, rssi: int | None, mem_pct: float | None, motion_detected: bool = False, show_time: bool = True, coordinates: list = [0, 0]):
    """Draws the Head-Up Display with separated floating boxes."""
    x, y = coordinates
    h, w = frame.shape[:2]
    # Configuration
    top_margin = 15
    box_h = 36
    pad_x = 12
    gap = 10
    
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55  # Increased slightly
    font_color = (230, 230, 230)
    thickness = 1
    # --- 1. Timestamp (Top Left) ---
    ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    (tw, th), baseline = cv2.getTextSize(ts, font, font_scale, thickness)
    ts_box_w = tw + (pad_x * 2)

    
    
    text_y = top_margin + (box_h + th) // 2 - 2
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
        draw_box(frame, gap, top_margin, ts_box_w, box_h)
        cv2.putText(frame, ts, (gap + pad_x, text_y), font, font_scale, font_color, thickness, cv2.LINE_AA)

    # --- 2. Motion Warning (Next to Timestamp) ---
    if motion_detected:
        warn_text = "MOTION DETECTED"
        (tw, th), _ = cv2.getTextSize(warn_text, font, font_scale, thickness)
        warn_box_w = tw + (pad_x * 2)
        warn_x = gap + ts_box_w + gap
        if should_draw("motion_warn", warn_x, top_margin, warn_box_w, box_h):
            draw_box(frame, warn_x, top_margin, warn_box_w, box_h, bg_color=(180, 40, 40), alpha=0.9)
            cv2.putText(frame, warn_text, (warn_x + pad_x, text_y), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
    
    # --- 3. Status Widgets (Top Right - Flowing Left) ---
    cursor_x = w - gap
    
    # -- WiFi Box --
    wifi_text = f"{rssi}dBm" if rssi is not None else "--dBm"
    (tw, th), _ = cv2.getTextSize(wifi_text, font, font_scale, thickness)
    
    icon_size = 20
    icon_pad = 8
    wifi_box_w = tw + icon_size + icon_pad + (pad_x * 2)
    
    cursor_x -= wifi_box_w
    if should_draw("wifi", cursor_x, top_margin, wifi_box_w, box_h):
        draw_box(frame, cursor_x, top_margin, wifi_box_w, box_h)

        # Draw content
        wifi_color = get_status_color(rssi, [-60, -70, -80], [(100, 255, 100), (0, 255, 255), (0, 165, 255), (50, 50, 255)])

        # Icon
        icon_x = cursor_x + pad_x
        draw_wifi_icon(frame, icon_x, top_margin + 6, icon_size, rssi, wifi_color)

        # Text
        cv2.putText(frame, wifi_text, (icon_x + icon_size + icon_pad, text_y), font, font_scale, font_color, thickness, cv2.LINE_AA)
    
    cursor_x -= gap
    
    # -- FPS Box --
    fps_val = int(fps)
    fps_str = f"{fps_val} fps"
    (tw, th), _ = cv2.getTextSize(fps_str, font, font_scale, thickness)
    
    fps_box_w = tw + (pad_x * 2) + 6 # +6 for dot space
    cursor_x -= fps_box_w
    if should_draw("fps", cursor_x, top_margin, fps_box_w, box_h):
        draw_box(frame, cursor_x, top_margin, fps_box_w, box_h)

        # Color logic: >= 7 Green, >= 5 Yellow, else Red
        fps_color = get_status_color(fps, [7, 5], [(100, 255, 100), (0, 255, 255), (50, 50, 255)])

        # Dot
        dot_x = cursor_x + pad_x
        dot_y = top_margin + box_h // 2
        cv2.circle(frame, (dot_x + 2, dot_y), 3, fps_color, -1)

        # Text
        cv2.putText(frame, fps_str, (dot_x + 10, text_y), font, font_scale, font_color, thickness, cv2.LINE_AA)
    
    cursor_x -= gap
    
    # -- Memory Box (if enabled) --
    if SHOW_MEMORY_BADGE:
        mem_val = f"{int(mem_pct)}%" if mem_pct is not None else "--%"
        (tw, th), _ = cv2.getTextSize(mem_val, font, font_scale, thickness)
        
        icon_w = 12
        icon_pad = 6
        mem_box_w = tw + icon_w + icon_pad + (pad_x * 2)
        
        cursor_x -= mem_box_w
        if should_draw("memory", cursor_x, top_margin, mem_box_w, box_h):
            draw_box(frame, cursor_x, top_margin, mem_box_w, box_h)

            mem_color = get_status_color(mem_pct, [20, 10], [(220, 220, 220), (0, 255, 255), (50, 50, 255)])

            # Icon (Simple Chip)
            ic_x = cursor_x + pad_x
            ic_y = top_margin + 10
            cv2.rectangle(frame, (ic_x, ic_y), (ic_x + icon_w, ic_y + 14), mem_color, 1)
            # Pins
            cv2.line(frame, (ic_x+2, ic_y+3), (ic_x+icon_w-2, ic_y+3), mem_color, 1)
            cv2.line(frame, (ic_x+2, ic_y+10), (ic_x+icon_w-2, ic_y+10), mem_color, 1)

            # Text
            cv2.putText(frame, mem_val, (ic_x + icon_w + icon_pad, text_y), font, font_scale, font_color, thickness, cv2.LINE_AA)
        
        cursor_x -= gap



def backoff(attempt: int) -> float:
    return min(5.0, 0.5 * (2 ** attempt))


def show_placeholder(message: str) -> None:
    if not SHOW_LOCAL_VIEW:
        return  # Don't show placeholder if local view is disabled
    base = no_signal_img if no_signal_img is not None else np.zeros((480, 640, 3), dtype=np.uint8)
    frame = base.copy()
    
    # Message
    cv2.putText(frame, message, (30, 100), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (200, 200, 200), 1, cv2.LINE_AA)
    
    # Use draw_hud with placeholders
    draw_hud(frame, fps=0, rssi=None, mem_pct=None)
    
    cv2.imshow("frame", frame)


def show_no_signal_frame(message: str) -> Optional[np.ndarray]:
    """Create and optionally display a no-signal frame. Always returns the frame for recording."""
    # Initialize frame from no_signal_img
    if no_signal_img is not None:
        frame = no_signal_img.copy()
    else:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(frame, "NO SIGNAL", (160, 260), cv2.FONT_HERSHEY_SIMPLEX,
                    1.4, (0, 0, 255), 3, cv2.LINE_AA)

    # Draw message below HUD area
    cv2.putText(frame, message, (30, 100), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (200, 200, 200), 1, cv2.LINE_AA)

    # Get current status values
    with rssi_lock:
        current_rssi = rssi_value
    with fps_lock:
        current_fps = fps_value
    with memory_lock:
        current_memory = memory_percent

    # Draw HUD
    draw_hud(frame, current_fps, current_rssi, current_memory)

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
        cv2.putText(base, "NO SIGNAL", (width//4, height//2), cv2.FONT_HERSHEY_SIMPLEX,
                    1.4, (0, 0, 255), 3, cv2.LINE_AA)

    frame = base.copy()
    
    # Draw message
    cv2.putText(frame, message, (30, 100), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (200, 200, 200), 1, cv2.LINE_AA)

    # Get current status values
    with rssi_lock:
        current_rssi = rssi_value
    with fps_lock:
        current_fps = fps_value
    with memory_lock:
        current_memory = memory_percent

    # Draw HUD
    draw_hud(frame, current_fps, current_rssi, current_memory)

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


def _open_capture_thread():
    """Open capture in background thread."""
    try:
        cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

        with capture_lock:
            capture_result['cap'] = cap
            capture_result['done'] = True
    except Exception as e:
        print(f"Exception opening capture: {e}")
        with capture_lock:
            capture_result['cap'] = None
            capture_result['done'] = True


def open_capture_with_timeout() -> Optional[cv2.VideoCapture]:
    """Open capture with timeout - if it takes too long, abort."""
    global capture_result

    # Reset state
    with capture_lock:
        capture_result = {'cap': None, 'done': False}

    # Start opening in background
    thread = threading.Thread(target=_open_capture_thread, daemon=True)
    thread.start()

    # Wait with timeout
    start_time = time.time()
    while time.time() - start_time < CAPTURE_OPEN_TIMEOUT:
        with capture_lock:
            if capture_result['done']:
                return capture_result['cap']
        time.sleep(0.1)

    # Timeout - abandon the thread and return None
    print(f"Capture open timed out after {CAPTURE_OPEN_TIMEOUT}s")
    return None


def main() -> None:
    global ffmpeg_record_proc, ffmpeg_rtsp_proc, expected_frame_size, current_fps, camera_adjustments_done
    attempt = 0
    cap = None

    # Initialize motion detection components
    mog2 = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    blinker = NonBlockingBlinker(blink_interval=0.5)

    # Ensure SIGALRM interrupts blocking frame reads after FRAME_READ_TIMEOUT seconds.
    class FrameReadTimeout(Exception):
        pass

    def _timeout_handler(_signum, _frame):
        raise FrameReadTimeout()

    signal.signal(signal.SIGALRM, _timeout_handler)

    print("Starting camera initialization in background...")
    if ENABLE_RECORDING:
        print(f"Recording enabled: {BASE_DIR}")
        if USE_DYNAMIC_FPS:
            print(f"Segment duration: {SEGMENT_SECONDS}s, FPS: Dynamic (matches source)")
        else:
            print(f"Segment duration: {SEGMENT_SECONDS}s, FPS: 10 (fixed)")
    if not SHOW_LOCAL_VIEW:
        print("Local view disabled - running in headless mode")
        print("Press Ctrl+C to stop")
    start_startup(force=True)
    # Start monitoring threads early so they show status during startup
    start_rssi_monitor()
    if SHOW_MEMORY_BADGE:
        start_memory_monitor()
    start_motion_logger()  # Start motion logging thread
    show_placeholder("STARTUP: Initializing camera...")
    cv2.waitKey(1)

    try:
        while True:
            # Only check for 'q' key if showing local view
            if SHOW_LOCAL_VIEW:
                if cv2.waitKey(1) == ord('q'):
                    break
            else:
                # Small sleep to prevent tight loop when not showing view
                time.sleep(0.01)

            if not startup_complete.is_set():
                if cap is not None:
                    cap.release()
                    cap = None

                # Show and record "no signal" frame during initialization
                # RSSI/Memory monitors are running, showing actual camera health
                record_no_signal_frame("STARTUP: Configuring camera...")

                time.sleep(0.05)
                continue

            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()

                # Show and record "no signal" frame during connection attempts
                # Startup is complete, but video stream not connected yet
                record_no_signal_frame(f"STREAM: Connecting (attempt {attempt + 1})...")

                cap = open_capture_with_timeout()
                if cap is None or not cap.isOpened():
                    print(f"Failed to open stream on attempt {attempt + 1}")
                    if cap is not None:
                        cap.release()
                    cap = None
                    attempt += 1
                    time.sleep(backoff(attempt))
                    start_startup(force=True)
                    continue
                print("Connection established.")
                attempt = 0

            try:
                signal.setitimer(signal.ITIMER_REAL, FRAME_READ_TIMEOUT)
                ret, frame = cap.read()
            except FrameReadTimeout:
                print("Frame read timed out - forcing restart.")
                ret, frame = False, None
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)

            if not ret or frame is None:
                print("Frame read failed - signal lost.")
                cap.release()
                cap = None
                start_startup(force=True)

                # Show and record "no signal" frame
                # Camera crashed during operation - restarting everything
                record_no_signal_frame("CRASH: Restarting camera...")

                time.sleep(FRAME_RETRY_DELAY)
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
            contours, _ = cv2.findContours(filtered_motion, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
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
                if ( 10 <= x <= 46 and 15 <= y <= 276):
                    time_overlap = True
                    print("time_overlap")

                # Only draw motion boxes if flag is enabled
                if SHOW_MOTION_BOXES:
                    cv2.rectangle(disp, (x, y), (x + w, y + h), (0, 255, 255), 2)
                    cx, cy = x + w // 2, y + h // 2
                    cv2.circle(disp, (cx, cy), 3, (0, 255, 255), -1)
                    cv2.putText(disp, f"motion {area:.0f}", (x, max(0, y - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)

            # Drive non-blocking blinker on motion
            if motion_detected and not blinker.is_active:
                blinker.start(duration=1)
                # Queue motion event for logging (with debounce)
                queue_motion_log(datetime.now(IST))

            # Update blinker, but catch errors if camera is not responding
            try:
                blinker.update()
            except Exception as e:
                # Camera might be having issues - trigger startup in background
                # but let the frame reading logic handle the actual reconnect
                print(f"Warning: Blinker update failed (camera may be crashed): {e}")
                start_startup(force=True)

            # Draw HUD (Timestamp, Status Badges, Motion Warning)
            with rssi_lock:
                current_rssi = rssi_value
            with fps_lock:
                current_fps = fps_value
            with memory_lock:
                current_memory = memory_percent

            draw_hud(disp, current_fps, current_rssi, current_memory, motion_detected, time_overlap, coordinates )

            # Draw ROI polygon on display only if flag is enabled
            if SHOW_MOTION_BOXES:
                cv2.polylines(disp, [ROI_PTS], isClosed=True, color=(0, 255, 255),
                              thickness=1, lineType=cv2.LINE_AA)

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
