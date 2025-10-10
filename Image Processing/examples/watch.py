import cv2
from datetime import datetime
import pytz

URL = "http://192.168.1.116:81/stream"  

cap = cv2.VideoCapture(URL)

if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")


ist = pytz.timezone('Asia/Kolkata')
while True:
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Stopped Receiving Frames")
    if cv2.waitKey(1) == ord('q'):
        break
    current_time = current_time = datetime.now(ist)
    formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S %p")
    cv2.putText(frame, formatted_time, (10 , 25), cv2.FONT_HERSHEY_SIMPLEX, 1.0,(0, 255, 0),  2, cv2.LINE_AA )
    cv2.imshow("frame", frame)

 
cap.release()
cv2.destroyAllWindows()