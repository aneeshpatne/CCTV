import cv2
import time
from datetime import datetime, timezone, timedelta

URL = "http://192.168.1.13:81/stream"

t0 = time.time()
frames = 0

def get_coordinates(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        print(f"Left Click at: X={x}, Y={y}")

def open_cap(url):
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        return None
    return cap

cap = open_cap(URL)
open_attempts = 0
read_attempts = 0
fps = 0.0

win_name = "ESP32-CAM (q/Esc to quit)" 
cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
cv2.setMouseCallback(win_name, get_coordinates)

while True:
    if cap is None:
        open_attempts += 1
        if open_attempts > 5:
            print("Max open attempts reached, exiting")
            break
        time.sleep(1.0)
        cap = open_cap(URL)
        continue
    else:
        open_attempts = 0

    ok, frame = cap.read()
    if not ok or frame is None:
        read_attempts += 1
        if read_attempts > 5:
            print("Max read attempts reached, exiting")
            break

        cap.release()
        cap = None
        time.sleep(0.5)
        continue
    else:
        read_attempts = 0


    frames += 1
    now = time.time()
    if now - t0 >= 1.0:
        fps = frames / (now - t0)
        frames, t0 = 0, now

    ist = timezone(timedelta(hours=5, minutes=30))
    ts = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

    cv2.putText(frame, f"{ts}  |  {fps:.1f} FPS", (10, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    cv2.imshow(win_name, frame)

    k = cv2.waitKey(1) & 0xFF
    if k == ord('q') or k == 27:
        break


if cap is not None:
    cap.release()
cv2.destroyAllWindows()
