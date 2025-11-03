import sys
import os
import logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import time
import requests
from requests.exceptions import RequestException

from tools.status import status
from tools.stream_status import check_mjpeg_stream
from tools.changeQuality import change_quality
from tools.reset import reset
from tools.changeClock import change_clock

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
count = 1


def startup():
    global count
    while True:
        cam_stat = check_mjpeg_stream()[0]
        stat = status()
        if cam_stat == False or stat == None:
            logger.warning(f"Camera Connection Failed Retrying, Attempt Number: {count}")
            count +=1
            reset()
            time.sleep(10)
            continue
        i = 1
        logger.info("Camera Initiated")
        logger.info(f"Initial Quality: {stat}")
        i = max(int(stat), 10)
        while i < 12:
            logger.info(f"Current Resolution: {i}")
            logger.info(f"Attempting to set Current Resolution to: {i + 1}")
            cam_stat = check_mjpeg_stream()[0]
            if cam_stat == False:
                logger.warning("Camera not ready")
                time.sleep(10)
                continue
            
            # Wrap change_quality in try-except to handle connection timeouts
            try:
                change_quality(i + 1)
                time.sleep(3)
            except RequestException as err:
                logger.warning(f"Failed to change quality (connection error): {err}")
                # Camera likely crashed - restart from beginning
                time.sleep(5)
                break
            except Exception as err:
                logger.warning(f"Unexpected error changing quality: {err}")
                time.sleep(5)
                break
            
            stat = status()
            cam_stat = check_mjpeg_stream()[0]
            if cam_stat == False or stat == None or int(stat) != i + 1:
                logger.warning("Resolution Change Failed")
                i = 10
                time.sleep(5)
                continue
            i += 1
        
        # Only log success if we actually completed the loop
        if i >= 12:
            time.sleep(6)
            logger.info(f"Resolution Set Successfully to {i}")
        else:
            logger.warning("Resolution setting incomplete - will retry")
            continue
        
        # Set camera clock
        try:
            logger.info("Setting camera clock to 20")
            change_clock(20)
        except RequestException as err:
            logger.warning(f"Setting camera clock failed: {err}")
        
        time.sleep(2)
        logger.info("Camera startup sequence completed")
        break


if __name__ == "__main__":
    startup()