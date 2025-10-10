import cv2

URL = "http://192.168.1.116:81/stream"  

cap = cv2.VideoCapture(URL)

if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")

while True:
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Stopped Receiving Frames")
    
    cv2.imshow("frame", frame)
    if cv2.waitKey(1) == ord('q'):
        break

 
cap.release()
cv2.destroyAllWindows()