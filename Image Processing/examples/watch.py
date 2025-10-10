import cv2

URL = "http://192.168.1.116:81/stream"  

cap = cv2.VideoCapture(URL)

if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")

while True:
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Stopped Receiving Frames")
    if cv2.waitKey(1) == ord('q'):
        break
    cv2.putText(frame, "Aneesh Cam", (10 , 25), cv2.FONT_HERSHEY_SIMPLEX, 1.0,(0, 255, 0),  2, cv2.LINE_AA )
    cv2.imshow("frame", frame)

 
cap.release()
cv2.destroyAllWindows()