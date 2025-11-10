from pathlib import Path
import logging
import requests
from datetime import datetime, timedelta
import pytz
from collections import defaultdict
import sys
import os
import json
import asyncio
import subprocess
import tempfile
from dotenv import load_dotenv
from telegram import Bot
from telegram.request import HTTPXRequest
from telegram.constants import ParseMode


load_dotenv()

# Telegram bot token
TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
WHITELIST_FILE = "whitelist.json"

def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r") as f:
            return set(json.load(f))
    return set()

whitelist = load_whitelist()


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

logging.info(f"[TELEGRAM] Commencing motion messages")

# Build a clean, readable message for Telegram
total_duration = sum((e.get('duration') or 0) for e in motion_events)

events_str = "\n".join(
    f"• {e.get('timestamp').strftime('%H:%M:%S')} — {e.get('duration'):.2f} min"
    for e in motion_events
)

message = (
    f"<b>todays events</b>\n"
    f"📅 Date: {now_ist}\n"
    f"⏱️ Time window: 00:00–07:00\n"
    f"🎯 Total events: {len(motion_events)}\n"
    f"⏳ Total duration: {total_duration:.2f} min\n\n"
    f"{events_str}"
)


async def send_telegram_notification(message: str):
    """Send notification to all whitelisted users"""
    request = HTTPXRequest(connection_pool_size=8, read_timeout=60, write_timeout=60, connect_timeout=30)
    bot = Bot(token=TOKEN, request=request)
    
    for user_id in whitelist:
        try:
            await bot.send_message(chat_id=user_id, text=message, parse_mode=ParseMode.HTML)  # type: ignore
            logging.info(f"[TELEGRAM] ✓ Sent to user {user_id}")
        except Exception as e:
            logging.error(f"[TELEGRAM] ✗ Failed to send to user {user_id}: {e}")


MAX_TELEGRAM_VIDEO_SIZE = 50 * 1024 * 1024 
TARGET_SIZE = 45 * 1024 * 1024  

def compress_video(input_path: Path, target_size_bytes: int = TARGET_SIZE) -> Path:
    """
    Compress video using ffmpeg to fit under target size.
    Returns path to compressed video (in temp directory).
    """
    input_size = input_path.stat().st_size
    input_size_mb = input_size / (1024 * 1024)
    target_size_mb = target_size_bytes / (1024 * 1024)
    
    logging.info(f"[COMPRESS] Starting compression of {input_path.name} ({input_size_mb:.2f} MB -> target: {target_size_mb:.2f} MB)")
    
    # Create a temporary file for compressed output
    temp_dir = Path(tempfile.gettempdir())
    output_path = temp_dir / f"compressed_{input_path.name}"
    
    try:

        duration_cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(input_path)
        ]
        result = subprocess.run(duration_cmd, capture_output=True, text=True, timeout=30)
        duration = float(result.stdout.strip())
        

        target_bitrate = int((target_size_bytes * 8 * 0.90) / duration / 1000)
        
        logging.info(f"[COMPRESS] Video duration: {duration:.2f}s, target bitrate: {target_bitrate} kbps")
        

        ffmpeg_cmd = [
            'ffmpeg', '-i', str(input_path),
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '28',  # Quality factor (higher = more compression, 23 is default)
            '-maxrate', f'{target_bitrate}k',
            '-bufsize', f'{target_bitrate * 2}k',
            '-c:a', 'aac',
            '-b:a', '96k',  # Lower audio bitrate
            '-movflags', '+faststart',
            '-y',  # Overwrite output file
            str(output_path)
        ]
        
        logging.info(f"[COMPRESS] Running ffmpeg compression...")
        subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=300)
        
        output_size = output_path.stat().st_size
        output_size_mb = output_size / (1024 * 1024)
        compression_ratio = (1 - output_size / input_size) * 100
        
        logging.info(f"[COMPRESS] ✓ Compressed to {output_size_mb:.2f} MB (saved {compression_ratio:.1f}%)")
        
        # Safety check: if still too large, try more aggressive compression
        if output_size > target_size_bytes:
            logging.warning(f"[COMPRESS] First pass still too large, applying aggressive compression...")
            
            # More aggressive settings
            aggressive_bitrate = int(target_bitrate * 0.7)
            ffmpeg_cmd_aggressive = [
                'ffmpeg', '-i', str(input_path),
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '32',  # Higher CRF for more compression
                '-maxrate', f'{aggressive_bitrate}k',
                '-bufsize', f'{aggressive_bitrate * 2}k',
                '-vf', 'scale=iw*0.8:ih*0.8',  # Reduce resolution by 20%
                '-c:a', 'aac',
                '-b:a', '64k',
                '-movflags', '+faststart',
                '-y',
                str(output_path)
            ]
            
            subprocess.run(ffmpeg_cmd_aggressive, capture_output=True, check=True, timeout=300)
            
            output_size = output_path.stat().st_size
            output_size_mb = output_size / (1024 * 1024)
            logging.info(f"[COMPRESS] ✓ Aggressive compression complete: {output_size_mb:.2f} MB")
        
        return output_path
        
    except subprocess.TimeoutExpired:
        logging.error(f"[COMPRESS] ✗ Compression timed out for {input_path.name}")
        raise
    except subprocess.CalledProcessError as e:
        logging.error(f"[COMPRESS] ✗ ffmpeg error: {e.stderr.decode() if e.stderr else str(e)}")
        raise
    except Exception as e:
        logging.error(f"[COMPRESS] ✗ Compression failed: {e}")
        raise

async def send_telegram_video(directory):
    request = HTTPXRequest(connection_pool_size=8, read_timeout=60, write_timeout=60, connect_timeout=30)
    bot = Bot(token=TOKEN, request=request)
    
    for file in directory.iterdir():
        if not file.is_file() or file.suffix.lower() != ".mp4":
            continue
            
        file_size = file.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        
        video_to_send = file
        temp_compressed = None
        
        try:
            logging.info(f"[TELEGRAM] Compressing {file.name} ({file_size_mb:.2f} MB)...")
            temp_compressed = compress_video(file, TARGET_SIZE)
            video_to_send = temp_compressed
            
            compressed_size_mb = temp_compressed.stat().st_size / (1024 * 1024)
            savings = ((file_size - temp_compressed.stat().st_size) / file_size) * 100
            logging.info(f"[TELEGRAM] Compressed {file.name}: {file_size_mb:.2f} MB → {compressed_size_mb:.2f} MB (saved {savings:.1f}%)")
            
            for user_id in whitelist:
                try:
                    with open(video_to_send, 'rb') as f:
                        await bot.send_video(chat_id=user_id, video=f)  # type: ignore
                    logging.info(f"[TELEGRAM] ✓ Sent {file.name} to user {user_id}")
                except Exception as e:
                    logging.error(f"[TELEGRAM] ✗ Failed to send {file.name} to user {user_id}: {e}")
                    
        except Exception as e:
            logging.error(f"[TELEGRAM] ✗ Error processing {file.name}: {e}")
        finally:
            if temp_compressed and temp_compressed.exists():
                try:
                    temp_compressed.unlink()
                    logging.info(f"[CLEANUP] Deleted temporary compressed file: {temp_compressed.name}")
                except Exception as e:
                    logging.error(f"[CLEANUP] Failed to delete temp file {temp_compressed}: {e}")
            


asyncio.run(send_telegram_notification(message))
asyncio.run(send_telegram_video(directory=directory))