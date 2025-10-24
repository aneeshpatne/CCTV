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

URL = "http://192.168.1.119:81/stream"
IST = pytz.timezone('Asia/Kolkata')

def open_capture():
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap

def backoff(a): 
    return min(5.0, 0.5 * (2 ** a))

attempt = 0
cap = None

# CRITICAL: Call startup() BEFORE first connection attempt
startup()

try:
    while True:
        # Ensure we have a valid capture object
        if cap is None or not cap.isOpened():
            print(f"Attempting to open stream (attempt {attempt + 1})...")
            if cap is not None:
                cap.release()
            
            cap = open_capture()
            
            if not cap.isOpened():
                print(f"Failed to open stream, retrying in {backoff(attempt):.1f}s...")
                time.sleep(backoff(attempt))
                attempt += 1
                
                # Call startup() on every failed connection attempt
                startup()
                continue
        
        # Try to read frame
        ret, frame = cap.read()
        
        if not ret or frame is None:
            print("Frame read failed - connection lost")
            cap.release()
            cap = None  # Force reconnection
            
            time.sleep(backoff(attempt))
            attempt += 1
            
            # Call startup() when connection is lost
            startup()
            continue
        
        # Success - reset attempt counter
        attempt = 0
        
        # Display frame with timestamp
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %p")
        cv2.putText(frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                    1.0, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.imshow("frame", frame)
        
        if cv2.waitKey(1) == ord('q'):
            break

finally:
    if cap is not None:
        cap.release()
    cv2.destroyAllWindows()