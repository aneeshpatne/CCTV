import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import time

from tools.status import status
from tools.stream_status import check_mjpeg_stream
from tools.adjustResolution import adjustResolution

def startup():
    while True:
        cam_stat = check_mjpeg_stream()
        print(cam_stat[0])
        stat = status()
        print(stat)
        if cam_stat == False or stat == None:
            print("Camera Connection Failed Retrying")
            time.sleep(2)
    

startup()