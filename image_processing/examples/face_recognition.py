import cv2
import time
from datetime import datetime, timezone, timedelta
import pytz
URL = "http://192.168.0.13:81/stream"  

t0 = time.time()
frames = 0


def open_cap(URL):
    cap = cv2.VideoCapture(URL)
    if not cap.isOpened():
        return None
    return cap
cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml" # type: ignore
face_cascade = cv2.CascadeClassifier(cascade_path)
if face_cascade.empty():
    raise RuntimeError(f"Failed to load face cascade at {cascade_path}")

cap = open_cap(URL)
attempt_count = 0
read_attempt_count = 0  
fps = 0.0

while True:
    if cap is None:
        attempt_count += 1
        if attempt_count > 5:
            print("Max attempts reached, exiting")
            break
        time.sleep(1)
        cap = open_cap(URL)
        continue
    ok, frame = cap.read()
    
    if not ok or frame is None:
        if read_attempt_count > 5: 
            if attempt_count > 5:
                print("Max attempts reached, exiting")
                break
        read_attempt_count+= 1
        cap.release()
        cap = None
        continue
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    for (x, y, w, h) in faces:
        cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
    frames += 1
    now = time.time()
    if now - t0 >= 1.0:
        fps = frames / (now - t0)
        frames, t0 = 0, now

    ist = timezone(timedelta(hours=5, minutes=30))
    current_time = datetime.now(ist)
    ts = current_time.strftime("%Y-%m-%d %H:%M:%S")
    
    cv2.putText(frame, f"{ts}  |  {fps:.1f} FPS", (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    cv2.imshow("ESP32-CAM (q=quit)", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

if cap is not None:
    cap.release()
cv2.destroyAllWindows()



