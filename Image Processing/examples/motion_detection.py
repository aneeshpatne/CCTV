import cv2
from datetime import datetime
import pytz
import numpy as np
import sys
sys.path.append('/home/aneesh/code/CCTV')
from utilities.warn import NonBlockingBlinker


URL = "http://192.168.1.116:81/stream"  
cap = cv2.VideoCapture(URL)

if not cap.isOpened():
    raise RuntimeError("Could Not Open Stream")

ist = pytz.timezone('Asia/Kolkata')



cv2.namedWindow("frame")


mog2 = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)

min_area = 800
blinker = NonBlockingBlinker(blink_interval=0.5)  # Create the non-blocking blinker
while True:
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Stopped Receiving Frames")
    
    fg_mask = mog2.apply(frame)
    _, mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, None)  # type: ignore
    mask = cv2.dilate(mask, None, iterations=2)  # type: ignore

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    disp = frame.copy()
    motion_detected = False
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        motion_detected = True
        x, y, w, h = cv2.boundingRect(c)
        cv2.rectangle(disp, (x, y), (x + w, y + h), (0, 255, 255), 2)
        cx, cy = x + w // 2, y + h // 2
        cv2.circle(disp, (cx, cy), 3, (0, 255, 255), -1)
        cv2.putText(disp, f"motion {area:.0f}", (x, max(0, y - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
    
    
    if motion_detected:
        # Start the blinker if not already active
        if not blinker.is_active:
            blinker.start(duration=1)
    
    # Update the blinker state every frame (non-blocking)
    blinker.update()
    
    current_time = datetime.now(ist)
    formatted_time = current_time.strftime("%Y-%m-%d %I:%M:%S %p")
    formatted_text = formatted_time + (" Motion Detected" if motion_detected else "")
    cv2.putText(disp, formatted_text, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 
                1.0, (0, 255, 0), 2, cv2.LINE_AA)
    
    cv2.imshow("frame", disp)

    
    if cv2.waitKey(1) == ord('q'):
        break
 
cap.release()
cv2.destroyAllWindows()