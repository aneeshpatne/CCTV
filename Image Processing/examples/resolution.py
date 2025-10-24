import os
import sys

# CRITICAL: Set FFMPEG options BEFORE importing cv2
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rtsp_transport;tcp|"
    "timeout;5000000|"         # 5s connection timeout (μs) - MUST be set before cv2 import
    "stimeout;5000000|"        # 5s socket timeout (μs)
    "rw_timeout;5000000|"      # 5s read/write timeout (μs)
    "listen_timeout;5000000|"  # 5s listen timeout (μs)
    "reconnect;1|"
    "reconnect_streamed;1|"
    "reconnect_at_eof;1|"
    "reconnect_on_network_error;1|"
    "reconnect_delay_max;2|"   # 2ms max reconnect delay
    "max_delay;5000000"        # 5s max delay
)

import threading
import time
import signal
from datetime import datetime

import cv2
import numpy as np
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from utilities.startup import startup

URL = "http://192.168.1.119:81/stream"
IST = pytz.timezone('Asia/Kolkata')
NO_SIGNAL_PATH = os.path.join(os.path.dirname(__file__), 'no_signal.png')
FRAME_RETRY_DELAY = 0.5
FRAME_READ_TIMEOUT = 5.0  # seconds
CAPTURE_OPEN_TIMEOUT = 10.0  # seconds to wait for capture to open

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


def backoff(attempt: int) -> float:
    return min(5.0, 0.5 * (2 ** attempt))


def show_placeholder(message: str) -> None:
    base = no_signal_img if no_signal_img is not None else np.zeros((480, 640, 3), dtype=np.uint8)
    frame = base.copy()
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %p")
    cv2.putText(frame, ts, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                0.75, (0, 255, 0), 2, cv2.LINE_AA)
    cv2.putText(frame, message, (10, 70), cv2.FONT_HERSHEY_SIMPLEX,
                0.75, (0, 165, 255), 2, cv2.LINE_AA)
    cv2.imshow("frame", frame)


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


def open_capture_with_timeout() -> cv2.VideoCapture:
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
    attempt = 0
    cap = None

    # Ensure SIGALRM interrupts blocking frame reads after FRAME_READ_TIMEOUT seconds.
    class FrameReadTimeout(Exception):
        pass

    def _timeout_handler(_signum, _frame):
        raise FrameReadTimeout()

    signal.signal(signal.SIGALRM, _timeout_handler)

    print("Starting camera initialization in background...")
    start_startup(force=True)
    show_placeholder("Initializing camera...")
    cv2.waitKey(1)

    try:
        while True:
            if cv2.waitKey(1) == ord('q'):
                break

            if not startup_complete.is_set():
                if cap is not None:
                    cap.release()
                    cap = None
                show_placeholder("Initializing camera...")
                time.sleep(0.05)
                continue

            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                show_placeholder(f"Connecting (attempt {attempt + 1})...")
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
                show_placeholder("Signal lost - restarting...")
                time.sleep(FRAME_RETRY_DELAY)
                continue

            ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %p")
            cv2.putText(frame, ts, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.9, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow("frame", frame)

    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()