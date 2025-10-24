import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from utilities.startup import startup

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rw_timeout;5000000|"
    "timeout;5000000|"
    "reconnect;1|"
    "reconnect_streamed;1|"
    "reconnect_at_eof;1|"
    "reconnect_on_network_error;1|"
    "reconnect_delay_max;2000"
)

import cv2
from datetime import datetime
import pytz
import time
import threading

URL = "http://192.168.1.119:81/stream"
IST = pytz.timezone('Asia/Kolkata')

# Load no signal image
NO_SIGNAL_PATH = os.path.join(os.path.dirname(__file__), 'no_signal.png')
no_signal_img = cv2.imread(NO_SIGNAL_PATH)
if no_signal_img is None:
    print(f"Warning: Could not load no_signal.png from {NO_SIGNAL_PATH}")
    # Create a simple black frame as fallback
    no_signal_img = cv2.zeros((480, 640, 3), dtype=cv2.uint8)
    cv2.putText(no_signal_img, "NO SIGNAL", (200, 240), 
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3, cv2.LINE_AA)

# Startup state tracking
startup_complete = threading.Event()
startup_thread = None

# Connection state tracking
connection_attempt_active = threading.Event()
connection_result = {'cap': None, 'success': False}
connection_lock = threading.Lock()

def run_startup():
    """Run startup in a separate thread"""
    attempt = 1
    while not startup_complete.is_set():
        try:
            print(f"Running startup attempt {attempt}...")
            startup()
            startup_complete.set()
            print("Startup completed successfully!")
        except Exception as e:
            print(f"Startup failed with error: {e}")
            print("Will retry startup in 5s...")
            time.sleep(5)
            attempt += 1
            # Loop will retry until success

def open_capture_thread():
    """Open capture in a separate thread to avoid blocking"""
    global connection_result
    try:
        cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        with connection_lock:
            connection_result['cap'] = cap
            connection_result['success'] = cap.isOpened()
    except Exception as e:
        print(f"Exception during capture open: {e}")
        with connection_lock:
            connection_result['cap'] = None
            connection_result['success'] = False
    finally:
        connection_attempt_active.clear()

def start_connection_attempt():
    """Start a non-blocking connection attempt"""
    connection_attempt_active.set()
    with connection_lock:
        connection_result['cap'] = None
        connection_result['success'] = False
    
    thread = threading.Thread(target=open_capture_thread, daemon=True)
    thread.start()
    return thread

def backoff(a): 
    return min(5.0, 0.5 * (2 ** a))

attempt = 0
cap = None
last_backoff_start = None
connection_thread = None

# Start startup in a separate thread (non-blocking)
print("Starting camera initialization in background...")
startup_thread = threading.Thread(target=run_startup, daemon=True)
startup_thread.start()

try:
    while True:
        # Check if we should quit
        key = cv2.waitKey(1)
        if key == ord('q'):
            break
        
        # Always update display with timestamp
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %p")
        
        # If startup is still running, show no signal image and do nothing else
        if not startup_complete.is_set():
            display_frame = no_signal_img.copy()
            cv2.putText(display_frame, "Initializing Camera...", (10, 60), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA)
            cv2.putText(display_frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                        0.7, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow("frame", display_frame)
            
            # Clean up any existing connection while waiting for startup
            if cap is not None:
                cap.release()
                cap = None
            # Cancel any pending connection attempts
            connection_attempt_active.clear()
            attempt = 0
            last_backoff_start = None
            continue
        
        # Check if we're in a backoff period
        if last_backoff_start is not None:
            elapsed = time.time() - last_backoff_start
            backoff_duration = backoff(attempt - 1)
            
            if elapsed < backoff_duration:
                # Still in backoff, show no signal
                display_frame = no_signal_img.copy()
                remaining = int(backoff_duration - elapsed) + 1
                cv2.putText(display_frame, f"Reconnecting in {remaining}s...", (10, 60), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA)
                cv2.putText(display_frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                            0.7, (0, 255, 0), 2, cv2.LINE_AA)
                cv2.imshow("frame", display_frame)
                continue
            else:
                # Backoff period over
                last_backoff_start = None
        
        # Check if connection attempt is in progress
        if connection_attempt_active.is_set():
            display_frame = no_signal_img.copy()
            cv2.putText(display_frame, f"Connecting... (attempt {attempt})", (10, 60), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA)
            cv2.putText(display_frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                        0.7, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow("frame", display_frame)
            continue
        
        # Check if we need to process connection result
        with connection_lock:
            if connection_result['cap'] is not None:
                if cap is not None:
                    cap.release()
                cap = connection_result['cap']
                success = connection_result['success']
                connection_result['cap'] = None
                connection_result['success'] = False
                
                if not success:
                    print(f"Connection attempt {attempt} failed - cap.isOpened() = False")
                    cap.release()
                    cap = None
                    attempt += 1
                    last_backoff_start = time.time()
                    
                    # Trigger startup IMMEDIATELY on connection failure
                    print("Triggering startup due to connection failure...")
                    startup_complete.clear()
                    startup_thread = threading.Thread(target=run_startup, daemon=True)
                    startup_thread.start()
                else:
                    print("Connection established (cap.isOpened() = True)")
                    # Don't reset attempt counter yet - wait for first successful frame read
                    # attempt = 0
        
        # Ensure we have a valid capture object
        if cap is None or not cap.isOpened():
            # CRITICAL: Do not attempt connection until startup is complete
            if not startup_complete.is_set():
                # Show no signal while waiting for startup
                display_frame = no_signal_img.copy()
                cv2.putText(display_frame, "Waiting for initialization...", (10, 60), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA)
                cv2.putText(display_frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                            0.7, (0, 255, 0), 2, cv2.LINE_AA)
                cv2.imshow("frame", display_frame)
                continue
            
            # Start connection attempt if not already in progress
            if not connection_attempt_active.is_set():
                print(f"Starting connection attempt {attempt + 1}...")
                attempt += 1
                connection_thread = start_connection_attempt()
            
            # Show no signal while waiting
            display_frame = no_signal_img.copy()
            cv2.putText(display_frame, "Waiting for connection...", (10, 60), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA)
            cv2.putText(display_frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                        0.7, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow("frame", display_frame)
            continue
        
        # Try to read frame
        ret, frame = cap.read()
        
        if not ret or frame is None:
            print("Frame read failed - signal lost!")
            cap.release()
            cap = None  # Force reconnection
            
            # Trigger startup IMMEDIATELY on signal loss
            print("Triggering startup due to signal loss...")
            startup_complete.clear()
            startup_thread = threading.Thread(target=run_startup, daemon=True)
            startup_thread.start()
            
            # Show no signal and loop back to wait for startup
            display_frame = no_signal_img.copy()
            cv2.putText(display_frame, "Signal Lost - Restarting...", (10, 60), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA)
            cv2.putText(display_frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                        0.7, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.imshow("frame", display_frame)
            continue
        
        # Success - reset attempt counter
        attempt = 0
        
        # Display frame with timestamp
        cv2.putText(frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                    1.0, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.imshow("frame", frame)

finally:
    if cap is not None:
        cap.release()
    cv2.destroyAllWindows()