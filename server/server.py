from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from pathlib import Path
import os
import socket
import subprocess
import tempfile
import hashlib
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utilities.motion_db import (
    get_motion_events_by_hours,
    get_motion_events_by_date,
    get_motion_events_by_range,
    get_total_motion_count
)

app = FastAPI(title="CCTV Video Server", version="1.0")

# Enable CORS for network access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure your CCTV footage directory
BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_FOLDER = Path(os.getenv("CCTV_TEMP_DIR", "/tmp/cctv_merged"))

def resolve_path(env_keys: list[str], fallback_paths: list[Path]) -> Path:
    for key in env_keys:
        value = os.getenv(key)
        if value:
            return Path(value).expanduser()
    for path in fallback_paths:
        if path.exists():
            return path
    return fallback_paths[0]


CCTV_FOLDER = resolve_path(
    ["CCTV_RECORDINGS_DIR", "RECORDINGS_DIR"],
    [
        Path("/Volumes/drive/CCTV/recordings/esp_cam1"),
        BASE_DIR / "recordings" / "esp_cam1",
    ],
)
NIGHT_EVENTS_FOLDER = resolve_path(
    ["MOTION_DATA_DIR", "DATA_DIR"],
    [
        Path("/Volumes/drive/CCTV/motion/data"),
        BASE_DIR / "motion" / "data",
    ],
)

# Create temp folder if it doesn't exist
TEMP_FOLDER.mkdir(parents=True, exist_ok=True)

@app.get("/")
async def root():
    """Root endpoint with server info"""
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    return {
        "message": "CCTV Video Server",
        "hostname": hostname,
        "local_ip": local_ip,
        "endpoints": {
            "list_videos": "/video/list",
            "last_videos": "/video/last?minutes=5|15|30|60",
            "by_timestamp": "/video/by-timestamp?timestamp=YYYY-MM-DDTHH:MM:SS",
            "by_duration": "/video/by-duration?timestamp=YYYY-MM-DDTHH:MM:SS&minutes=X",
            "by_hour": "/video/by-hour?timestamp=YYYY-MM-DDTHH:MM:SS",
            "by_day": "/video/by-day?timestamp=YYYY-MM-DDTHH:MM:SS",
            "stream_file": "/video/stream/{filename}",
            "motion_logs": "/motion/logs?hours=1|12|24",
            "motion_by_day": "/motion/day?date=YYYY-MM-DD",
            "motion_by_range": "/motion/range?start=ISO&end=ISO",
            "motion_stats": "/motion/stats",
            "night_events_list": "/nightevents",
            "night_event_by_index": "/nightevents/{index}",
            "docs": "/docs"
        }
    }


def find_videos_in_range(start_time: datetime, end_time: datetime) -> list[Path]:
    """Find all videos within a time range."""
    folder = Path(CCTV_FOLDER)
    videos = []
    
    for file in folder.glob("recording_*.mp4"):
        try:
            timestamp_str = file.stem.replace("recording_", "")
            dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            
            if start_time <= dt <= end_time:
                videos.append((dt, file))
        except ValueError:
            continue
    
    # Sort by timestamp
    videos.sort(key=lambda x: x[0])
    return [v[1] for v in videos]


def merge_videos(video_files: list[Path], output_path: Path) -> bool:
    """Merge multiple video files using ffmpeg."""
    if not video_files:
        return False
    
    # Create a text file listing all videos
    list_file = output_path.parent / f"{output_path.stem}_list.txt"
    
    try:
        with open(list_file, 'w') as f:
            for video in video_files:
                # Escape single quotes and write in ffmpeg concat format
                f.write(f"file '{str(video)}'\n")
        
        # Use ffmpeg to concatenate videos
        cmd = [
            'ffmpeg',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(list_file),
            '-c', 'copy',  # Copy without re-encoding (faster)
            '-y',  # Overwrite output file
            str(output_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Clean up list file
        list_file.unlink()
        
        return result.returncode == 0
        
    except Exception as e:
        if list_file.exists():
            list_file.unlink()
        raise e


def cleanup_old_merged_videos():
    """Clean up merged videos older than 1 hour."""
    temp_folder = Path(TEMP_FOLDER)
    current_time = datetime.now().timestamp()
    
    for file in temp_folder.glob("merged_*.mp4"):
        if current_time - file.stat().st_mtime > 3600:  # 1 hour
            try:
                file.unlink()
            except:
                pass


def find_video_by_timestamp(timestamp: datetime) -> Path:
    """
    Find video file matching the given timestamp.
    Expected filename format: recording_YYYYMMDD_HHMMSS.mp4
    """
    # Format the timestamp to match your filename pattern
    filename = f"recording_{timestamp.strftime('%Y%m%d_%H%M%S')}.mp4"
    filepath = Path(CCTV_FOLDER) / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"Video not found for timestamp: {timestamp}")
    
    return filepath


@app.get("/video/by-duration")
async def get_video_by_duration(timestamp: str, minutes: int, background_tasks: BackgroundTasks):
    """
    Get merged video for a custom duration starting from a timestamp.
    Includes extra videos at the start and end to ensure complete coverage.
    
    Args:
        timestamp: ISO format datetime (e.g., "2025-11-05T19:40:00")
                  Starting point for the video duration
        minutes: Duration in minutes (e.g., 30, 60, 90, 120)
                Will include 1 video before start and 1 video after end
    
    Example:
        /video/by-duration?timestamp=2025-11-05T19:40:00&minutes=60
        /video/by-duration?timestamp=2025-11-05T19:40:00&minutes=30
    """
    try:
        # Parse timestamp
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        
        # Calculate time range
        start_time = dt
        end_time = dt + timedelta(minutes=minutes)

        # Get all videos so we can handle timestamps with seconds
        folder = Path(CCTV_FOLDER)
        all_videos = []

        for file in folder.glob("recording_*.mp4"):
            try:
                timestamp_str = file.stem.replace("recording_", "")
                file_dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                all_videos.append((file_dt, file))
            except ValueError:
                continue

        if not all_videos:
            raise HTTPException(
                status_code=404,
                detail="No videos found in the recordings folder"
            )

        all_videos.sort(key=lambda x: x[0])

        start_idx = None
        end_idx = None

        for i, (file_dt, file) in enumerate(all_videos):
            if start_idx is None and file_dt >= start_time:
                start_idx = max(0, i - 1)
            if file_dt > end_time:
                end_idx = max(0, i - 1)
                break

        if start_idx is None:
            start_idx = len(all_videos) - 1
        if end_idx is None:
            end_idx = len(all_videos) - 1

        # Include 1 video before and after the range
        start_idx = max(0, start_idx - 1)
        end_idx = min(len(all_videos) - 1, end_idx + 1)

        if start_idx > end_idx:
            raise HTTPException(
                status_code=404,
                detail=f"No videos found for period: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        videos_to_merge = [all_videos[i][1] for i in range(start_idx, end_idx + 1)]
        
        # If only one video, return it directly
        if len(videos_to_merge) == 1:
            return FileResponse(
                path=str(videos_to_merge[0]),
                media_type="video/mp4",
                filename=videos_to_merge[0].name
            )
        
        # Generate unique filename for merged video
        timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
        merged_filename = f"merged_duration_{minutes}min_{timestamp_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename
        
        # Check if merged video already exists
        if not merged_path.exists():
            # Merge videos
            if not merge_videos(videos_to_merge, merged_path):
                raise HTTPException(status_code=500, detail="Failed to merge videos")
        
        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)
        
        return FileResponse(
            path=str(merged_path),
            media_type="video/mp4",
            filename=merged_filename
        )
        
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-11-05T19:40:00)"
        )


@app.get("/video/by-hour")
async def get_video_by_hour(timestamp: str, background_tasks: BackgroundTasks):
    """
    Get merged video for a specific hour.
    
    Args:
        timestamp: ISO format datetime (e.g., "2025-10-29T10:00:00")
                  Will return all videos from that hour (10:00:00 to 10:59:59)
    
    Example:
        /video/by-hour?timestamp=2025-10-29T10:00:00
    """
    try:
        # Parse timestamp
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        
        # Get hour range
        start_time = dt.replace(minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(hours=1) - timedelta(seconds=1)
        
        # Find videos in this hour
        videos = find_videos_in_range(start_time, end_time)
        
        if not videos:
            raise HTTPException(
                status_code=404, 
                detail=f"No videos found for hour: {start_time.strftime('%Y-%m-%d %H:00')}"
            )
        
        # If only one video, return it directly
        if len(videos) == 1:
            return FileResponse(
                path=str(videos[0]),
                media_type="video/mp4",
                filename=videos[0].name
            )
        
        # Generate unique filename for merged video
        hour_str = start_time.strftime("%Y%m%d_%H")
        merged_filename = f"merged_hour_{hour_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename
        
        # Check if merged video already exists
        if not merged_path.exists():
            # Merge videos
            if not merge_videos(videos, merged_path):
                raise HTTPException(status_code=500, detail="Failed to merge videos")
        
        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)
        
        return FileResponse(
            path=str(merged_path),
            media_type="video/mp4",
            filename=merged_filename
        )
        
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-10-29T10:00:00)"
        )


@app.get("/video/by-day")
async def get_video_by_day(timestamp: str, background_tasks: BackgroundTasks):
    """
    Get merged video for a specific day.
    
    Args:
        timestamp: ISO format datetime (e.g., "2025-10-29T00:00:00")
                  Will return all videos from that day (00:00:00 to 23:59:59)
    
    Example:
        /video/by-day?timestamp=2025-10-29T00:00:00
    """
    try:
        # Parse timestamp
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        
        # Get day range
        start_time = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=1) - timedelta(seconds=1)
        
        # Find videos in this day
        videos = find_videos_in_range(start_time, end_time)
        
        if not videos:
            raise HTTPException(
                status_code=404,
                detail=f"No videos found for day: {start_time.strftime('%Y-%m-%d')}"
            )
        
        # If only one video, return it directly
        if len(videos) == 1:
            return FileResponse(
                path=str(videos[0]),
                media_type="video/mp4",
                filename=videos[0].name
            )
        
        # Generate unique filename for merged video
        day_str = start_time.strftime("%Y%m%d")
        merged_filename = f"merged_day_{day_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename
        
        # Check if merged video already exists
        if not merged_path.exists():
            # Merge videos
            if not merge_videos(videos, merged_path):
                raise HTTPException(status_code=500, detail="Failed to merge videos")
        
        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)
        
        return FileResponse(
            path=str(merged_path),
            media_type="video/mp4",
            filename=merged_filename
        )
        
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-10-29T00:00:00)"
        )


@app.get("/video/last")
async def get_last_videos(minutes: int, background_tasks: BackgroundTasks):
    """
    Get merged video for the last N minutes from now.
    
    Args:
        minutes: Number of minutes to look back (5, 15, 30, or 60)
    
    Example:
        /video/last?minutes=5
        /video/last?minutes=15
        /video/last?minutes=30
        /video/last?minutes=60
    """
    if minutes not in [5, 15, 30, 60]:
        raise HTTPException(
            status_code=400,
            detail="Minutes must be 5, 15, 30, or 60"
        )
    
    try:
        # Calculate time range
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=minutes)
        
        # Find all videos in the time range
        folder = Path(CCTV_FOLDER)
        videos = []
        
        for file in sorted(folder.glob("recording_*.mp4")):
            try:
                timestamp_str = file.stem.replace("recording_", "")
                dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                
                # Include videos that started before or during our time range
                # This ensures we don't cut off videos weirdly
                if dt <= end_time and dt >= start_time - timedelta(minutes=10):
                    videos.append((dt, file))
            except ValueError:
                continue
        
        if not videos:
            raise HTTPException(
                status_code=404,
                detail=f"No videos found for the last {minutes} minutes"
            )
        
        # Sort by timestamp
        videos.sort(key=lambda x: x[0])
        video_files = [v[1] for v in videos]
        
        # If only one video, return it directly
        if len(video_files) == 1:
            return FileResponse(
                path=str(video_files[0]),
                media_type="video/mp4",
                filename=video_files[0].name
            )
        
        # Generate unique filename for merged video
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        merged_filename = f"merged_last_{minutes}min_{timestamp_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename
        
        # Merge videos
        if not merge_videos(video_files, merged_path):
            raise HTTPException(status_code=500, detail="Failed to merge videos")
        
        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)
        
        return FileResponse(
            path=str(merged_path),
            media_type="video/mp4",
            filename=merged_filename
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/video/by-timestamp")
async def get_video_by_timestamp(timestamp: str, background_tasks: BackgroundTasks):
    """
    Get video that contains the requested timestamp, including 1 video before and 1 video after.
    Finds the video whose recording period would contain the given timestamp.
    
    Args:
        timestamp: ISO format datetime string (e.g., "2025-10-29T10:53:42")
                  or custom format "YYYY-MM-DD HH:MM:SS"
    
    Example:
        /video/by-timestamp?timestamp=2025-10-29T10:53:42
        /video/by-timestamp?timestamp=2025-10-29 10:53:42
    """
    try:
        # Try parsing ISO format first
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except:
            # Try parsing custom format
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        
        # Get all videos
        folder = Path(CCTV_FOLDER)
        all_videos = []
        
        for file in folder.glob("recording_*.mp4"):
            try:
                timestamp_str = file.stem.replace("recording_", "")
                file_dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                all_videos.append((file_dt, file))
            except ValueError:
                continue
        
        if not all_videos:
            raise HTTPException(
                status_code=404,
                detail="No videos found in the recordings folder"
            )
        
        # Sort by timestamp
        all_videos.sort(key=lambda x: x[0])
        
        # Find the video that would contain this timestamp
        # This is the video that starts at or before the requested time
        # and the next video starts after the requested time (or it's the last video)
        target_index = None
        
        for i, (file_dt, file) in enumerate(all_videos):
            # If this video starts after our timestamp, the previous one contains it
            if file_dt > dt:
                target_index = max(0, i - 1)
                break
        
        # If we didn't find a video starting after, the last video contains it
        if target_index is None:
            target_index = len(all_videos) - 1
        
        # Get videos: 1 before, target, 1 after
        videos_to_merge = []
        start_idx = max(0, target_index - 1)
        end_idx = min(len(all_videos), target_index + 2)
        
        for i in range(start_idx, end_idx):
            videos_to_merge.append(all_videos[i][1])
        
        # If only one video, return it directly
        if len(videos_to_merge) == 1:
            return FileResponse(
                path=str(videos_to_merge[0]),
                media_type="video/mp4",
                filename=videos_to_merge[0].name
            )
        
        # Generate unique filename for merged video
        timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
        merged_filename = f"merged_timestamp_{timestamp_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename
        
        # Check if merged video already exists
        if not merged_path.exists():
            # Merge videos
            if not merge_videos(videos_to_merge, merged_path):
                raise HTTPException(status_code=500, detail="Failed to merge videos")
        
        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)
        
        return FileResponse(
            path=str(merged_path),
            media_type="video/mp4",
            filename=merged_filename
        )
        
    except ValueError as e:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid timestamp format. Use ISO format (2025-10-29T10:53:42) or YYYY-MM-DD HH:MM:SS"
        )


@app.get("/video/stream/{filename}")
async def stream_video(filename: str):
    """
    Stream video by exact filename.
    
    Example:
        /video/stream/recording_20251029_105804.mp4
    """
    filepath = Path(CCTV_FOLDER) / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    
    if not filename.endswith('.mp4'):
        raise HTTPException(status_code=400, detail="Only MP4 files are supported")
    
    return FileResponse(
        path=str(filepath),
        media_type="video/mp4",
        filename=filename
    )


@app.get("/video/list")
async def list_videos():
    """
    List all available video recordings with their timestamps.
    """
    try:
        folder = Path(CCTV_FOLDER)
        videos = []
        
        for file in folder.glob("recording_*.mp4"):
            # Extract timestamp from filename
            try:
                timestamp_str = file.stem.replace("recording_", "")
                dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                videos.append({
                    "filename": file.name,
                    "timestamp": dt.isoformat(),
                    "size_mb": round(file.stat().st_size / (1024 * 1024), 2)
                })
            except ValueError:
                continue
        
        videos.sort(key=lambda x: x['timestamp'], reverse=True)
        return {"videos": videos, "count": len(videos)}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# MOTION DETECTION API ENDPOINTS
# ============================================================================

@app.get("/motion/logs")
async def get_motion_logs(hours: int = 24):
    """
    Get motion detection events from the last N hours.
    
    Args:
        hours: Number of hours to look back (default: 24)
                Common values: 1 (last hour), 12 (last 12 hours), 24 (last day)
    
    Example:
        /motion/logs?hours=1   # Last hour
        /motion/logs?hours=12  # Last 12 hours
        /motion/logs?hours=24  # Last 24 hours (default)
    """
    try:
        events = get_motion_events_by_hours(hours)
        return {
            "hours": hours,
            "count": len(events),
            "events": [e.to_dict() for e in events]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/day")
async def get_motion_by_day(date: str):
    """
    Get motion detection events for a specific day.
    
    Args:
        date: Date in YYYY-MM-DD format
    
    Example:
        /motion/day?date=2025-10-31
    """
    try:
        # Parse date
        dt = datetime.strptime(date, "%Y-%m-%d")
        events = get_motion_events_by_date(dt)
        
        return {
            "date": date,
            "count": len(events),
            "events": [e.to_dict() for e in events]
        }
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Use YYYY-MM-DD (e.g., 2025-10-31)"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/range")
async def get_motion_by_range(start: str, end: str):
    """
    Get motion detection events within a specific time range.
    
    Args:
        start: Start timestamp in ISO format (e.g., 2025-10-31T10:00:00)
        end: End timestamp in ISO format (e.g., 2025-10-31T12:00:00)
    
    Example:
        /motion/range?start=2025-10-31T10:00:00&end=2025-10-31T12:00:00
    """
    try:
        # Parse timestamps
        try:
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        except:
            start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        
        try:
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
        except:
            end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        
        if start_dt >= end_dt:
            raise HTTPException(
                status_code=400,
                detail="Start time must be before end time"
            )
        
        events = get_motion_events_by_range(start_dt, end_dt)
        
        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "count": len(events),
            "events": [e.to_dict() for e in events]
        }
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-10-31T10:00:00)"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/stats")
async def get_motion_stats():
    """
    Get overall motion detection statistics.
    
    Example:
        /motion/stats
    """
    try:
        total = get_total_motion_count()
        
        # Get counts for different time periods
        last_hour = len(get_motion_events_by_hours(1))
        last_12_hours = len(get_motion_events_by_hours(12))
        last_24_hours = len(get_motion_events_by_hours(24))
        
        return {
            "total_events": total,
            "last_hour": last_hour,
            "last_12_hours": last_12_hours,
            "last_24_hours": last_24_hours
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NIGHT EVENTS API ENDPOINTS
# ============================================================================

@app.get("/nightevents")
async def get_night_events():
    """
    Get all night event videos.
    Returns list of videos from the motion/data folder.
    Files are named by index (1.mp4, 2.mp4, etc.)

    Example:
        /nightevents
    """
    try:
        folder = Path(NIGHT_EVENTS_FOLDER)

        if not folder.exists():
            raise HTTPException(
                status_code=404,
                detail="Night events folder not found"
            )

        videos = []

        for file in folder.glob("*.mp4"):
            try:
                idx = int(file.stem)
                videos.append({
                    "index": idx,
                    "filename": file.name,
                    "size_mb": round(file.stat().st_size / (1024 * 1024), 2),
                })
            except (ValueError, OSError):
                continue

        return {
            "count": len(videos),
            "videos": videos
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/nightevents/{index}")
async def get_night_event_by_index(index: int):
    """
    Get a specific night event video by its index (1-based).

    Args:
        index: 1-based index of the video

    Example:
        /nightevents/1
        /nightevents/5
    """
    try:
        folder = Path(NIGHT_EVENTS_FOLDER)

        if not folder.exists():
            raise HTTPException(
                status_code=404,
                detail="Night events folder not found"
            )

        video_file = folder / f"{index}.mp4"

        if not video_file.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Video {index} not found"
            )

        return FileResponse(
            path=str(video_file),
            media_type="video/mp4",
            filename=video_file.name
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    
    # Get local IP address
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    
    print("\n" + "="*60)
    print("🎥 CCTV Video Server Starting...")
    print("="*60)
    print(f"📍 Local access:   http://127.0.0.1:8000")
    print(f"🌐 Network access: http://{local_ip}:8000")
    print(f"📚 API docs:       http://{local_ip}:8000/docs")
    print("="*60 + "\n")
    
    # Run server accessible on all network interfaces
    uvicorn.run(
        app, 
        host="0.0.0.0",  # Listen on all network interfaces
        port=8005,
        log_level="info"
    )
