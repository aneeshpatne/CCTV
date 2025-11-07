from pathlib import Path
import logging
import requests
from datetime import datetime, timedelta
import pytz

logging.basicConfig(level=logging.INFO)

ist = pytz.timezone('Asia/Kolkata')

logging.info("Program started")
directory = Path("data/") 

logging.info("Cleaning Paths")

# Deleting Paths


if directory.exists() and directory.is_dir():
    for file in directory.iterdir():
        if (file.is_file()):
            try:
                logging.info(f"[DELETE] Program started {file}")
                file.unlink()
            except Exception as e:
                logging.error(f"[DELETE] Failed to delete {file}: {e}")
else:
    logging.info("[DELETE] Directory Does not exist")

now_ist = datetime.now(ist).date()

logging.info(f"[FETCH] fetching Motions between 12 am to 7 am on {now_ist}")

data = requests.get(f"http://192.168.1.100:8005/motion/range?start={now_ist}T00:00:00&end={now_ist}T07:00:00")
timestamps = []
for d in data.json().get("events"):
    dt = datetime.fromisoformat(d.get('timestamp'))
    timestamps.append(dt)
    logging.info(f"Motion Detected at {dt.time()}")


logging.info("Merging Time")


for i in range(len(timestamps) - 1):
    diff = timestamps[i+1] - timestamps[i]        # timedelta
    print(diff, diff.total_seconds(), "seconds")