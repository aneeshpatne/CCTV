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

from cctv_telegram.message import send_message

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

    interval_events = []
    for d in events:
        try:
            start_raw = d.get("start_time")
            end_raw = d.get("end_time")
            duration_raw = d.get("duration")

            if not start_raw:
                continue

            start_dt = datetime.fromisoformat(start_raw)
            if end_raw:
                end_dt = datetime.fromisoformat(end_raw)
            else:
                duration_seconds = float(duration_raw or 0)
                end_dt = start_dt + timedelta(seconds=duration_seconds)

            interval_events.append(
                {
                    "start_time": start_dt,
                    "end_time": end_dt,
                }
            )
            logging.info(
                f"[FETCH] Motion event {start_dt.time()} -> {end_dt.time()} "
                f"({(end_dt - start_dt).total_seconds():.0f}s)"
            )
        except (ValueError, TypeError):
            continue

    if not interval_events:
        logging.warning("[FETCH] No valid interval events found")
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

interval_events.sort(key=lambda x: x["start_time"])

motion_events = []
for event in interval_events:
    if not motion_events:
        motion_events.append(event.copy())
        continue

    last = motion_events[-1]
    if event["start_time"] <= last["end_time"] + timedelta(minutes=2):
        if event["end_time"] > last["end_time"]:
            last["end_time"] = event["end_time"]
    else:
        motion_events.append(event.copy())

for item in motion_events:
    duration_seconds = (item["end_time"] - item["start_time"]).total_seconds()
    logging.info(
        f"[MERGE] Motion event: {item['start_time'].time()} - "
        f"Duration: {duration_seconds / 60:.2f} minutes"
    )

logging.info(f"[MERGE] Total merged motion events: {len(motion_events)}")

logging.info("[DOWNLOAD] Starting video downloads")
successful_downloads = 0
failed_downloads = 0

for idx, item in enumerate(motion_events, 1):
    try:
        event_start = item.get("start_time")
        event_end = item.get("end_time")
        if event_start is None or event_end is None:
            raise ValueError("Missing start_time/end_time in merged event")

        logging.info(
            f"[DOWNLOAD] ({idx}/{len(motion_events)}) Fetching video for motion "
            f"{event_start.time()} -> {event_end.time()}"
        )

        video_url = f"{API_BASE_URL}/video/v2/by-event?start={event_start.isoformat()}&end={event_end.isoformat()}"
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
total_duration = (
    sum((e["end_time"] - e["start_time"]).total_seconds() for e in motion_events) / 60
)

events_str = "\n".join(
    f"{idx} - {e.get('start_time').strftime('%H:%M:%S')} — "
    f"{((e.get('end_time') - e.get('start_time')).total_seconds() / 60):.2f} min\n"
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
