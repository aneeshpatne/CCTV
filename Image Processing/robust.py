import cv2

URL = "http://192.168.1.116:81/stream"  

def open_cap(URL):
    cap = cv2.VideoCapture(URL)
    if not cap.isOpened():
        return None
    return cap
