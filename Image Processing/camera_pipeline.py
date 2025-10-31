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
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utilities.startup import startup
from utilities.warn import NonBlockingBlinker
from tools.get_rssi import get_rssi
from utilities.motion_db import log_motion_event

URL = "http://192.168.1.119:81/stream"
IST = pytz.timezone('Asia/Kolkata')
NO_SIGNAL_PATH = os.path.join(os.path.dirname(__file__), 'examples', 'no_signal.png')
FRAME_RETRY_DELAY = 0.5
FRAME_READ_TIMEOUT = 5.0  # seconds
CAPTURE_OPEN_TIMEOUT = 10.0  # seconds to wait for capture to open

# Recording configuration
ENABLE_RECORDING = True
BASE_DIR = "/media/aneesh/SSD/recordings/esp_cam1"
SEGMENT_SECONDS = 3 * 60  # 1 minute per segment
RTSP_OUT = "rtsp://127.0.0.1:8554/esp_cam1_overlay"
ENABLE_RTSP = True  # Set to True if you want RTSP streaming
RECORD_FPS = 10  # Target FPS for recording

# Display configuration
SHOW_MOTION_BOXES = False  # Show motion detection boxes and ROI polygon
SHOW_LOCAL_VIEW = False    # Show CV2 preview windows

# Motion detection configuration
MIN_AREA = 800
ROI_PTS = np.array([
    [147, 400], [151, 427], [146, 487], [148, 524], [143, 557], [191, 551],
    [222, 560], [269, 561], [302, 553], [345, 555], [376, 556], [434, 546],
    [468, 550], [504, 545], [564, 541], [609, 543], [651, 542], [701, 544],
    [737, 538], [779, 536], [811, 535], [832, 506], [836, 475], [843, 471],
    [858, 457], [858, 440], [855, 413], [846, 391], [841, 352], [836, 329],
    [819, 319], [799, 271], [808, 238], [809, 201], [799, 192], [786, 191],
    [759, 194], [738, 194], [692, 196], [659, 200], [612, 201], [572, 197],
    [517, 194], [463, 197], [408, 208], [393, 236], [363, 236], [329, 233],
    [273, 230], [264, 232], [249, 259], [230, 273], [196, 289], [179, 292],
    [150, 291], [128, 315], [142, 339], [146, 363], [146, 381],
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

# Capture opening state
capture_result = {'cap': None, 'done': False}
capture_lock = threading.Lock()

# RSSI monitoring state
rssi_value = None
rssi_lock = threading.Lock()
rssi_thread = None
rssi_update_interval = 60  # Update RSSI once per minute

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
last_frame_time = 0.0
expected_frame_size: Optional[tuple[int, int]] = None  # (width, height) that FFmpeg expects


def start_ffmpeg_record(width: int, height: int, fps: int) -> Optional[subprocess.Popen]:
    """Start FFmpeg process responsible for local segmented recording."""
    os.makedirs(BASE_DIR, exist_ok=True)
    out_pattern = os.path.join(BASE_DIR, "recording_%Y%m%d_%H%M%S.mp4")

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",

        # raw frames over stdin
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps),
        "-fflags", "+genpts",
        "-i", "-",

        "-map", "0:v",
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-g", str(int(fps)),
        "-bf", "2",
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
        log_path = os.path.join(BASE_DIR, "ffmpeg_record.log")
        logf = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=logf,          # keep stderr for diagnostics
            bufsize=0
        )
        print(f"FFmpeg recording started: {out_pattern}")
        return proc
    except Exception as e:
        print(f"Failed to start FFmpeg: {e}")
        return None


def start_ffmpeg_rtsp(width: int, height: int, fps: int) -> Optional[subprocess.Popen]:
    """Start FFmpeg process responsible for RTSP/WebRTC restream."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",

        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps),
        "-fflags", "+genpts",
        "-i", "-",

        "-map", "0:v",
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-b:v", "1.5M",
        "-maxrate", "1.5M",
        "-bufsize", "3M",
        "-g", str(int(fps)),
        "-bf", "0",
        "-sc_threshold", "0",
        "-rtsp_transport", "tcp",
        "-f", "rtsp",
        RTSP_OUT,
    ]

    try:
        log_path = os.path.join(BASE_DIR, "ffmpeg_rtsp.log")
        logf = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=logf,
            bufsize=0
        )
        print(f"FFmpeg RTSP started: {RTSP_OUT}")
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
    global ffmpeg_record_proc, ffmpeg_rtsp_proc, last_frame_time, expected_frame_size

    if not ENABLE_RECORDING and not ENABLE_RTSP:
        return True

    with ffmpeg_lock:
        h, w = frame.shape[:2]
        new_size = (w, h)

        # Track the canonical size expected by the encoders
        if expected_frame_size is None:
            expected_frame_size = new_size
        elif new_size != expected_frame_size:
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

        target_width, target_height = expected_frame_size

        # Ensure recording process is alive when recording enabled
        if ENABLE_RECORDING:
            if ffmpeg_record_proc is not None and ffmpeg_record_proc.poll() is not None:
                exit_code = ffmpeg_record_proc.poll()
                print(f"Recording FFmpeg exited (code {exit_code}); restarting...")
                stop_ffmpeg(ffmpeg_record_proc)
                ffmpeg_record_proc = None
            if ffmpeg_record_proc is None:
                ffmpeg_record_proc = start_ffmpeg_record(target_width, target_height, RECORD_FPS)

        # Ensure RTSP process is alive when enabled
        if ENABLE_RTSP:
            if ffmpeg_rtsp_proc is not None and ffmpeg_rtsp_proc.poll() is not None:
                exit_code = ffmpeg_rtsp_proc.poll()
                print(f"RTSP FFmpeg exited (code {exit_code}); restarting...")
                stop_ffmpeg(ffmpeg_rtsp_proc)
                ffmpeg_rtsp_proc = None
            if ffmpeg_rtsp_proc is None:
                ffmpeg_rtsp_proc = start_ffmpeg_rtsp(target_width, target_height, RECORD_FPS)

        # If the current frame size differs from the expected size, resize once for both outputs
        if (w, h) != expected_frame_size:
            frame = cv2.resize(frame, expected_frame_size)

        # Rate limit to target FPS (shared across both pipes)
        now = time.time()
        if last_frame_time > 0:
            elapsed = now - last_frame_time
            target_interval = 1.0 / RECORD_FPS
            if elapsed < target_interval:
                time.sleep(target_interval - elapsed)
        last_frame_time = time.time()

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
                return starter(target_width, target_height, RECORD_FPS)
            return proc

        if ENABLE_RECORDING:
            ffmpeg_record_proc = _write(ffmpeg_record_proc, "recording", start_ffmpeg_record)
        if ENABLE_RTSP:
            ffmpeg_rtsp_proc = _write(ffmpeg_rtsp_proc, "rtsp", start_ffmpeg_rtsp)

        return True


def start_startup(force: bool = False) -> None:
    global startup_thread
    with startup_lock:
        if force:
            startup_complete.clear()
        if startup_complete.is_set():
            return
        if startup_thread is None or not startup_thread.is_alive():
            def _runner() -> None:
                attempt = 1
                while not startup_complete.is_set():
                    try:
                        print(f"Running startup attempt {attempt}...")
                        startup()
                        startup_complete.set()
                        print("Startup completed successfully!")
                    except Exception as exc:
                        print(f"Startup failed with error: {exc}")
                        print("Retrying startup in 5 s...")
                        time.sleep(5)
                        attempt += 1

            startup_thread = threading.Thread(target=_runner, daemon=True)
            startup_thread.start()


def start_rssi_monitor() -> None:
    """Start background thread to monitor RSSI once per minute."""
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


def draw_wifi_signal(frame: np.ndarray, rssi: int | None) -> None:
    """Draw WiFi signal strength indicator with CCTV-style background.
    
    Args:
        frame: Frame to draw on (modified in-place)
        rssi: Signal strength in dBm (e.g., -50) or None
    """
    # Position in top-right corner
    badge_w = 120
    badge_h = 30
    badge_x = frame.shape[1] - badge_w - 10
    badge_y = 10
    
    # Draw semi-transparent dark background
    overlay = frame.copy()
    cv2.rectangle(overlay, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h),
                 (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    
    # Determine signal strength and color
    if rssi is None:
        bars_filled = 0
        color = (0, 0, 255)  # Red for no signal
        text = "N/A"
        border_color = (0, 0, 255)
    elif rssi >= -50:
        bars_filled = 4
        color = (0, 255, 0)  # Green for excellent
        text = f"{rssi} dBm"
        border_color = (0, 255, 0)
    elif rssi >= -60:
        bars_filled = 3
        color = (0, 255, 0)  # Green for good
        text = f"{rssi} dBm"
        border_color = (0, 255, 0)
    elif rssi >= -70:
        bars_filled = 2
        color = (0, 255, 255)  # Yellow for fair
        text = f"{rssi} dBm"
        border_color = (0, 255, 255)
    elif rssi >= -80:
        bars_filled = 1
        color = (0, 165, 255)  # Orange for weak
        text = f"{rssi} dBm"
        border_color = (0, 165, 255)
    else:
        bars_filled = 0
        color = (0, 0, 255)  # Red for very weak
        text = f"{rssi} dBm"
        border_color = (0, 0, 255)
    
    # Draw border with color based on signal strength
    cv2.rectangle(frame, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h),
                 border_color, 1)
    
    # Draw 4 bars inside badge
    bar_width = 6
    bar_spacing = 3
    bar_height_base = 5
    x_base = badge_x + 12
    y_base = badge_y + badge_h - 8
    
    for i in range(4):
        x = x_base + i * (bar_width + bar_spacing)
        bar_height = bar_height_base + (i + 1) * 3
        y_top = y_base - bar_height
        
        if i < bars_filled:
            # Filled bar
            cv2.rectangle(frame, (x, y_top), (x + bar_width, y_base), color, -1)
        else:
            # Empty bar (outline only)
            cv2.rectangle(frame, (x, y_top), (x + bar_width, y_base), (80, 80, 80), 1)
    
    # Draw RSSI value text
    cv2.putText(frame, text, (badge_x + 50, badge_y + 19), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)


def backoff(attempt: int) -> float:
    return min(5.0, 0.5 * (2 ** attempt))


def show_placeholder(message: str) -> None:
    if not SHOW_LOCAL_VIEW:
        return  # Don't show placeholder if local view is disabled
    base = no_signal_img if no_signal_img is not None else np.zeros((480, 640, 3), dtype=np.uint8)
    frame = base.copy()
    ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    
    # Draw timestamp with CCTV-style background (matching normal frames)
    time_x = 10
    time_y = 10
    time_w = 305
    time_h = 30
    
    # Draw semi-transparent dark background for timestamp
    overlay = frame.copy()
    cv2.rectangle(overlay, (time_x, time_y), (time_x + time_w, time_y + time_h),
                 (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    
    # Draw subtle border
    cv2.rectangle(frame, (time_x, time_y), (time_x + time_w, time_y + time_h),
                 (0, 255, 0), 1)
    
    # Draw timestamp text
    cv2.putText(frame, ts, (time_x + 15, time_y + 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 255, 200), 1, cv2.LINE_AA)
    
    # Draw message below
    cv2.putText(frame, message, (10, 70), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (180, 220, 255), 1, cv2.LINE_AA)
    cv2.imshow("frame", frame)


def show_no_signal_frame(message: str) -> Optional[np.ndarray]:
    """Create and optionally display a no-signal frame. Always returns the frame for recording."""
    base = no_signal_img if no_signal_img is not None else np.zeros((480, 640, 3), dtype=np.uint8)
    frame = base.copy()
    ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    
    # Draw timestamp with CCTV-style background (matching normal frames)
    time_x = 10
    time_y = 10
    time_w = 305
    time_h = 30
    
    # Draw semi-transparent dark background for timestamp
    overlay = frame.copy()
    cv2.rectangle(overlay, (time_x, time_y), (time_x + time_w, time_y + time_h),
                 (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    
    # Draw subtle border
    cv2.rectangle(frame, (time_x, time_y), (time_x + time_w, time_y + time_h),
                 (0, 255, 0), 1)
    
    # Draw timestamp text
    cv2.putText(frame, ts, (time_x + 15, time_y + 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 255, 200), 1, cv2.LINE_AA)
    
    # Draw message below
    cv2.putText(frame, message, (10, 70), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (180, 220, 255), 1, cv2.LINE_AA)

    # Draw WiFi signal strength indicator
    with rssi_lock:
        current_rssi = rssi_value
    draw_wifi_signal(frame, current_rssi)

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
    ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
    
    # Draw timestamp with CCTV-style background (matching normal frames)
    time_x = 10
    time_y = 10
    time_w = 305
    time_h = 30
    
    # Draw semi-transparent dark background for timestamp
    overlay = frame.copy()
    cv2.rectangle(overlay, (time_x, time_y), (time_x + time_w, time_y + time_h),
                 (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    
    # Draw subtle border
    cv2.rectangle(frame, (time_x, time_y), (time_x + time_w, time_y + time_h),
                 (0, 255, 0), 1)
    
    # Draw timestamp text
    cv2.putText(frame, ts, (time_x + 15, time_y + 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 255, 200), 1, cv2.LINE_AA)
    
    # Draw message below
    cv2.putText(frame, message, (10, 70), cv2.FONT_HERSHEY_SIMPLEX,
                0.65, (180, 220, 255), 1, cv2.LINE_AA)

    # Draw WiFi signal strength indicator
    with rssi_lock:
        current_rssi = rssi_value
    draw_wifi_signal(frame, current_rssi)

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
    global ffmpeg_record_proc, ffmpeg_rtsp_proc, expected_frame_size
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
        print(f"Segment duration: {SEGMENT_SECONDS}s, FPS: {RECORD_FPS}")
    if not SHOW_LOCAL_VIEW:
        print("Local view disabled - running in headless mode")
        print("Press Ctrl+C to stop")
    start_startup(force=True)
    start_rssi_monitor()  # Start RSSI monitoring thread
    start_motion_logger()  # Start motion logging thread
    show_placeholder("Initializing camera...")
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
                record_no_signal_frame("Initializing camera...")

                time.sleep(0.05)
                continue

            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()

                # Show and record "no signal" frame during connection attempts
                record_no_signal_frame(f"Connecting (attempt {attempt + 1})...")

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
                record_no_signal_frame("Signal lost - restarting...")

                time.sleep(FRAME_RETRY_DELAY)
                continue

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
            for c in contours:
                area = cv2.contourArea(c)
                if area < MIN_AREA:
                    continue
                motion_detected = True
                # Only draw motion boxes if flag is enabled
                if SHOW_MOTION_BOXES:
                    x, y, w, h = cv2.boundingRect(c)
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

            # Timestamp and motion label
            ts = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p")
            
            # Draw timestamp with CCTV-style background
            time_x = 10
            time_y = 10
            time_w = 305
            time_h = 30
            
            # Draw semi-transparent dark background for timestamp
            overlay = disp.copy()
            cv2.rectangle(overlay, (time_x, time_y), (time_x + time_w, time_y + time_h),
                         (20, 20, 20), -1)
            cv2.addWeighted(overlay, 0.6, disp, 0.4, 0, disp)
            
            # Draw subtle border
            cv2.rectangle(disp, (time_x, time_y), (time_x + time_w, time_y + time_h),
                         (0, 255, 0), 1)
            
            # Draw timestamp text
            cv2.putText(disp, ts, (time_x + 15, time_y + 21),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.65, (200, 255, 200), 1, cv2.LINE_AA)
            
            # Draw subtle motion detection badge if motion detected (beside timestamp)
            if motion_detected:
                # Calculate position next to timestamp
                badge_x = 325  # Position to the right of timestamp
                badge_y = 10
                badge_w = 190
                badge_h = 30
                
                # Draw semi-transparent dark background rectangle
                overlay = disp.copy()
                cv2.rectangle(overlay, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h),
                             (40, 40, 40), -1)
                cv2.addWeighted(overlay, 0.5, disp, 0.5, 0, disp)
                
                # Draw subtle border
                cv2.rectangle(disp, (badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h),
                             (100, 200, 255), 1)
                
                # Draw small warning dot
                dot_x = badge_x + 12
                dot_y = badge_y + badge_h // 2
                cv2.circle(disp, (dot_x, dot_y), 5, (100, 200, 255), -1)
                
                # Draw "MOTION DETECTED" text
                cv2.putText(disp, "MOTION DETECTED", (badge_x + 25, badge_y + 21),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 220, 255), 1, cv2.LINE_AA)

            # Draw WiFi signal strength indicator
            with rssi_lock:
                current_rssi = rssi_value
            draw_wifi_signal(disp, current_rssi)

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
