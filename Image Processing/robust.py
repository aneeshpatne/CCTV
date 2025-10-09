import cv2
import time
URL = "http://192.168.1.116:81/stream"  

t0 = time.time()
frames = 0


def open_cap(URL):
    cap = cv2.VideoCapture(URL)
    if not cap.isOpened():
        return None
    return cap

cap = open_cap(URL)
attempt_count = 0
read_attempt_count = 0  

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
    frames += 1
    now = time.time()
    if now - t0 >= 1.0:
        fps = frames / (now - t0)
        frames, t0 = 0, now



