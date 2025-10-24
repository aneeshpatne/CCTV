import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.status import status
from tools.stream_status import check_mjpeg_stream
from tools.adjustResolution import adjustResolution

def startup():
    stat = status()
    print(stat)

startup()