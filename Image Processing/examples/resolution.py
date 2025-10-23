import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rw_timeout;5000000|"          # 5s read timeout (microseconds)
    "timeout;5000000|"             # 5s I/O timeout (some builds read this)
    "reconnect;1|"
    "reconnect_streamed;1|"
    "reconnect_at_eof;1|"
    "reconnect_on_network_error;1|"
    "reconnect_delay_max;2000"     # cap reconnect delay at 2s
)

import cv2
from datetime import datetime
import pytz, time

URL = "http://192.168.1.119:81/stream"  # ESP32-CAM MJPEG
IST = pytz.timezone('Asia/Kolkata')

def open_capture():
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    # keep buffer tiny to avoid stale frames
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap

cap = open_capture()
if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")

attempt = 0
def backoff(a): return min(5.0, 0.5 * (2 ** a))

try:
    while True:
        ret, frame = cap.read()
        if not ret or frame is None:
            print("CCTV Failed or Crashed")
            cap.release()
            time.sleep(backoff(attempt)); attempt += 1
            cap = open_capture()
            continue

        attempt = 0
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %p")
        cv2.putText(frame, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,0), 2, cv2.LINE_AA)
        cv2.imshow("frame", frame)
        if cv2.waitKey(1) == ord('q'):
            break
finally:
    cap.release()
    cv2.destroyAllWindows()
