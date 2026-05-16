import cv2
from tools.jpeg_ws_capture import JPEG_WS_URL, JpegWebSocketCapture

URL = JPEG_WS_URL

cap = JpegWebSocketCapture(URL)
cap.open()

if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")

while True:
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Stopped Receiving Frames")
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(grey, 100, 200)
    cv2.imshow("frame", edges)
    if cv2.waitKey(1) == ord("q"):
        break


cap.release()
cv2.destroyAllWindows()
