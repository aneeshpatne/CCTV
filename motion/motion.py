from pathlib import Path
import logging
import requests
from datetime import datetime, timedelta
import pytz
import sys
import os
import json
import asyncio
from dotenv import load_dotenv

load_dotenv()

from telegram.message import send_message

TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
WHITELIST_FILE = "whitelist.json"
API_BASE_URL = "http://127.0.0.1:8005"


def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r") as f:
            return set(json.load(f))
    return set()


whitelist = load_whitelist()


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

ist = pytz.timezone("Asia/Kolkata")

logging.info("=" * 50)
logging.info("Motion Detection Video Processor Started")
logging.info("=" * 50)

BASE_DIR = Path(__file__).resolve().parents[1]
data_dir = os.getenv("DATA_DIR") or os.getenv("MOTION_DATA_DIR")
directory = Path(data_dir).expanduser() if data_dir else (BASE_DIR / "motion" / "data")

# Create directory if it doesn't exist
try:
    directory.mkdir(exist_ok=True)
    logging.info(f"[SETUP] Data directory ready: {directory.absolute()}")
except Exception as e:
    logging.error(f"[SETUP] Failed to create directory: {e}")
    sys.exit(1)

logging.info("[CLEANUP] Starting cleanup of old files")

# Deleting old files
deleted_count = 0
if directory.exists() and directory.is_dir():
    for file in directory.iterdir():
        if file.is_file():
            try:
                file_size = file.stat().st_size / (1024 * 1024)
                logging.info(f"[CLEANUP] Deleting {file.name} ({file_size:.2f} MB)")
                file.unlink()
                deleted_count += 1
            except Exception as e:
                logging.error(f"[CLEANUP] Failed to delete {file}: {e}")
    logging.info(f"[CLEANUP] Deleted {deleted_count} old file(s)")

now_ist = datetime.now(ist).date()

logging.info(f"[FETCH] Fetching motion events between 12:00 AM to 7:00 AM on {now_ist}")

try:
    api_url = (
        f"{API_BASE_URL}/motion/range?start={now_ist}T00:00:00&end={now_ist}T07:00:00"
    )
    data = requests.get(api_url, timeout=30)
    data.raise_for_status()

    events = data.json().get("events", [])

    if not events:
        logging.warning("[FETCH] No motion events found")
        message = (
            "<b>Tonights events</b>\n"
            f"📅 Date: {now_ist}\n"
            "⏱️ Time window: 00:00–07:00\n"
            "🎯 Total events: 0\n"
            "⏳ Total duration: 0.00 min\n\n"
            "No motion events detected."
        )
        asyncio.run(send_message(message))
        sys.exit(0)

    logging.info(f"[FETCH] Retrieved {len(events)} motion event(s)")

    timestamps = []
    for d in events:
        try:
            dt = datetime.fromisoformat(d.get("timestamp"))
            timestamps.append(dt)
            logging.info(f"[FETCH] Motion detected at {dt.time()}")
        except (ValueError, TypeError):
            continue

    if not timestamps:
        logging.warning("[FETCH] No valid timestamps found")
        message = (
            "<b>Tonights events</b>\n"
            f"📅 Date: {now_ist}\n"
            "⏱️ Time window: 00:00–07:00\n"
            "🎯 Total events: 0\n"
            "⏳ Total duration: 0.00 min\n\n"
            "No motion events detected."
        )
        asyncio.run(send_message(message))
        sys.exit(0)

except requests.RequestException as e:
    logging.error(f"[FETCH] Request failed: {e}")
    sys.exit(1)
except Exception as e:
    logging.error(f"[FETCH] Unexpected error: {e}")
    sys.exit(1)


logging.info("[MERGE] Starting to merge nearby motion events")

motion_events = []
i = 0
while i < len(timestamps):
    start_time = timestamps[i]
    duration = 1
    j = i + 1
    while j < len(timestamps):
        diff = timestamps[j] - timestamps[j - 1]
        if diff < timedelta(minutes=2):
            duration += diff.total_seconds() / 60
            j += 1
        else:
            break

    motion_events.append({"timestamp": start_time, "duration": duration})

    logging.info(
        f"[MERGE] Motion event: {start_time.time()} - Duration: {duration:.2f} minutes"
    )
    i = j if j < len(timestamps) else len(timestamps)

logging.info(f"[MERGE] Total merged motion events: {len(motion_events)}")

logging.info("[DOWNLOAD] Starting video downloads")
successful_downloads = 0
failed_downloads = 0

for idx, item in enumerate(motion_events, 1):
    try:
        start_time = item.get("timestamp") - timedelta(minutes=1)
        duration = item.get("duration")

        logging.info(
            f"[DOWNLOAD] ({idx}/{len(motion_events)}) Fetching video for motion at {start_time.time()}"
        )

        video_url = f"{API_BASE_URL}/video/by-duration?timestamp={start_time.isoformat()}&minutes={int(duration)}"
        res = requests.get(video_url, timeout=120)
        res.raise_for_status()

        output_path = directory / f"{idx}.mp4"

        with open(output_path, "wb") as f:
            f.write(res.content)

        file_size = len(res.content) / (1024 * 1024)
        logging.info(
            f"[DOWNLOAD] ✓ Video saved: {output_path.name} ({file_size:.2f} MB)"
        )
        successful_downloads += 1

    except requests.RequestException as e:
        logging.error(f"[DOWNLOAD] ✗ Failed to fetch video: {e}")
        failed_downloads += 1
    except IOError as e:
        logging.error(f"[DOWNLOAD] ✗ Failed to write file: {e}")
        failed_downloads += 1
    except Exception as e:
        logging.error(f"[DOWNLOAD] ✗ Unexpected error: {e}")
        failed_downloads += 1

logging.info("=" * 50)
logging.info(
    f"[SUMMARY] Download complete: {successful_downloads} successful, {failed_downloads} failed"
)
logging.info("=" * 50)

logging.info("[TELEGRAM] Sending motion summary")

# Build Telegram message
total_duration = sum(e.get("duration", 0) for e in motion_events)

events_str = "\n".join(
    f"{idx} - {e.get('timestamp').strftime('%H:%M:%S')} — {e.get('duration'):.2f} min\n"
    f"http://192.168.0.99:8005/nightevents/{idx}"
    for idx, e in enumerate(motion_events, start=1)
)

message = (
    f"<b>Tonights events</b>\n"
    f"📅 Date: {now_ist}\n"
    f"⏱️ Time window: 00:00–07:00\n"
    f"🎯 Total events: {len(motion_events)}\n"
    f"⏳ Total duration: {total_duration:.2f} min\n\n"
    f"{events_str}"
)


asyncio.run(send_message(message))
logging.info("Motion Detection Video Processor Complete")
