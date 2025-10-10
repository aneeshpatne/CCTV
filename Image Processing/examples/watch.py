import cv2

URL = "http://192.168.1.116:81/stream"  

cap = cv2.VideoCapture(URL)

if not cap.isOpened():
    raise RuntimeError("Could not open video stream. Check Wi-Fi, URL, and that the camera is streaming.")

while True:
    ok, frame = cap.read()
    if not ok:
        print("Frame grab failed — trying again...")
        cv2.waitKey(30)
        continue
    frame = cv2.rotate(frame, cv2.ROTATE_180)
    cv2.imshow("ESP32-CAM", frame)


    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()