from pathlib import Path
import logging
import requests
from datetime import datetime, timedelta
import pytz
from collections import defaultdict
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

ist = pytz.timezone('Asia/Kolkata')

logging.info("="*50)
logging.info("Motion Detection Video Processor Started")
logging.info("="*50)

directory = Path("data/") 

# Create directory if it doesn't exist
try:
    directory.mkdir(exist_ok=True)
    logging.info(f"[SETUP] Data directory ready: {directory.absolute()}")
except Exception as e:
    logging.error(f"[SETUP] Failed to create directory {directory}: {e}")
    sys.exit(1)

logging.info("[CLEANUP] Starting cleanup of old files")

# Deleting old files
deleted_count = 0
try:
    if directory.exists() and directory.is_dir():
        for file in directory.iterdir():
            if file.is_file():
                try:
                    file_size = file.stat().st_size / (1024 * 1024)  # Size in MB
                    logging.info(f"[CLEANUP] Deleting {file.name} ({file_size:.2f} MB)")
                    file.unlink()
                    deleted_count += 1
                except Exception as e:
                    logging.error(f"[CLEANUP] Failed to delete {file}: {e}")
        logging.info(f"[CLEANUP] Deleted {deleted_count} old file(s)")
    else:
        logging.warning("[CLEANUP] Directory does not exist")
except Exception as e:
    logging.error(f"[CLEANUP] Error during cleanup: {e}")

now_ist = datetime.now(ist).date()

logging.info(f"[FETCH] Fetching motion events between 12:00 AM to 7:00 AM on {now_ist}")

try:
    api_url = f"http://192.168.1.100:8005/motion/range?start={now_ist}T00:00:00&end={now_ist}T07:00:00"
    logging.info(f"[FETCH] API URL: {api_url}")
    
    data = requests.get(api_url, timeout=30)
    data.raise_for_status()
    
    response_json = data.json()
    events = response_json.get("events", [])
    
    if not events:
        logging.warning("[FETCH] No motion events found for the specified time range")
        sys.exit(0)
    
    logging.info(f"[FETCH] Retrieved {len(events)} motion event(s)")
    
    timestamps = []
    for d in events:
        try:
            dt = datetime.fromisoformat(d.get('timestamp'))
            timestamps.append(dt)
            logging.info(f"[FETCH] Motion detected at {dt.time()}")
        except Exception as e:
            logging.error(f"[FETCH] Failed to parse timestamp {d.get('timestamp')}: {e}")
            continue
    
    if not timestamps:
        logging.warning("[FETCH] No valid timestamps found")
        sys.exit(0)
        
except requests.exceptions.Timeout:
    logging.error("[FETCH] Request timed out while fetching motion events")
    sys.exit(1)
except requests.exceptions.ConnectionError:
    logging.error("[FETCH] Failed to connect to the API server")
    sys.exit(1)
except requests.exceptions.HTTPError as e:
    logging.error(f"[FETCH] HTTP error occurred: {e}")
    sys.exit(1)
except Exception as e:
    logging.error(f"[FETCH] Unexpected error while fetching motion events: {e}")
    sys.exit(1)


logging.info("[MERGE] Starting to merge nearby motion events")

motion_events = []

i = 0
while i < len(timestamps):
    start_time = timestamps[i]
    duration = 1
    j = i + 1
    while j < len(timestamps):
        diff = timestamps[j] - timestamps[j-1]
        if diff < timedelta(minutes=2):
            duration += diff.total_seconds() / 60  
            j += 1
        else:
            break
    
    motion_events.append({
        'timestamp': start_time,
        'duration': duration
    })
    
    logging.info(f"[MERGE] Motion event: {start_time.time()} - Duration: {duration:.2f} minutes")
    
    i = j if j < len(timestamps) else len(timestamps)

logging.info(f"[MERGE] Total merged motion events: {len(motion_events)}")

logging.info("[DOWNLOAD] Starting video downloads")
successful_downloads = 0
failed_downloads = 0

for idx, item in enumerate(motion_events, 1):
    start_time = None
    output_path = None
    res = None
    
    try:
        start_time = item.get("timestamp") - timedelta(minutes=1)
        duration = item.get("duration")
        
        logging.info(f"[DOWNLOAD] ({idx}/{len(motion_events)}) Fetching video for motion at {start_time.time()} (Duration: {duration:.2f} min)")
        
        video_url = f"http://192.168.1.100:8005/video/by-duration?timestamp={start_time.isoformat()}&minutes={int(duration)}"
        
        res = requests.get(video_url, timeout=120)
        res.raise_for_status()
        
        output_path = directory / f"{item.get('timestamp')}.mp4"
        
        with open(output_path, "wb") as f:
            f.write(res.content)
        
        file_size = len(res.content) / (1024 * 1024)  # Size in MB
        logging.info(f"[DOWNLOAD] ✓ Video saved: {output_path.name} ({file_size:.2f} MB)")
        successful_downloads += 1
        
    except requests.exceptions.Timeout:
        time_str = start_time.time() if start_time else "unknown"
        logging.error(f"[DOWNLOAD] ✗ Timeout while fetching video for {time_str}")
        failed_downloads += 1
    except requests.exceptions.ConnectionError:
        time_str = start_time.time() if start_time else "unknown"
        logging.error(f"[DOWNLOAD] ✗ Connection error while fetching video for {time_str}")
        failed_downloads += 1
    except requests.exceptions.HTTPError as e:
        time_str = start_time.time() if start_time else "unknown"
        status_code = res.status_code if res else "unknown"
        logging.error(f"[DOWNLOAD] ✗ HTTP error {status_code} for video at {time_str}")
        failed_downloads += 1
    except IOError as e:
        file_str = output_path if output_path else "unknown file"
        logging.error(f"[DOWNLOAD] ✗ Failed to write file {file_str}: {e}")
        failed_downloads += 1
    except Exception as e:
        time_str = start_time.time() if start_time else "unknown"
        logging.error(f"[DOWNLOAD] ✗ Unexpected error for video at {time_str}: {e}")
        failed_downloads += 1

logging.info("="*50)
logging.info(f"[SUMMARY] Download complete: {successful_downloads} successful, {failed_downloads} failed")
logging.info("="*50)
    