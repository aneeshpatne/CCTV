import cv2, time

URL = "http://192.168.1.116:81/stream"

def open_cap(url):
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        return None
    return cap

cap = open_cap(URL)
last_ok = time.time()
frames = 0
fps = 0.0
t0 = time.time()

while True:
    if cap is None:
        time.sleep(1)
        cap = open_cap(URL)
        continue

    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        cap = None
        continue

    frames += 1
    now = time.time()
    if now - t0 >= 1.0:
        fps = frames / (now - t0)
        frames, t0 = 0, now

    # overlay timestamp + FPS
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(frame, f"{ts}  |  {fps:.1f} FPS", (10, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    cv2.imshow("ESP32-CAM (q=quit)", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

if cap is not None:
    cap.release()
cv2.destroyAllWindows()
