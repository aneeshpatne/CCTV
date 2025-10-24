import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import time

from tools.status import status
from tools.stream_status import check_mjpeg_stream
from tools.changeQuality import change_quality

count = 1
def startup():
    global count
    while True:
        cam_stat = check_mjpeg_stream()
        stat = status()
        if cam_stat == False or stat == None:
            print("[STARTUP] Camera Connection Failed Retrying, Attempt Number:" , count)
            count +=1
            time.sleep(2)
            continue
        print("[STARTUP] Camera Initiated")
        print("[STARTUP] Initial Quality: ", stat)
        i = int(stat) 
        while i < 12:
            print("[STARTUP] Current Resolution: ", i)
            print("[STARTUP] Attempting to set Current Resolution to: ", i + 1)
            change_quality(i + 1)
            time.sleep(2)
            stat = status()
            cam_stat = check_mjpeg_stream()
            if cam_stat == False or stat == None or int(stat) != i + 1:
                print("[STARTUP] Resolution Change Failed")
                i = 6
                continue
            i += 1
        print("[STARTUP] Resolution Set Successfully", i )
        break
        
startup()