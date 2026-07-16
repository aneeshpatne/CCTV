from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Callable, Iterator
import atexit
import os
import socket
import subprocess
import hashlib
import threading
import uuid

from utilities.motion_db_new import (
    get_motion_events_by_hours,
    get_motion_events_by_date,
    get_motion_events_by_range,
    get_motion_counts,
    get_motion_event_stats_per_hour,
    get_motion_event_stats_per_hour_last_month,
    get_motion_annotations,
)
from utilities.recording_catalog import RecordingCatalog

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
STATIC_FOLDER = BASE_DIR / "server" / "static"
TEMP_FOLDER = Path(os.getenv("CCTV_TEMP_DIR", "/tmp/cctv_merged"))
LIVE_STREAM_URL = os.getenv(
    "CCTV_LIVE_STREAM_URL",
    "http://192.168.0.112:8889/esp_cam1_overlay/",
)
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
ESP32CAM_RECOVERY_REDIS_KEY = "esp32cam:recovery"


def resolve_path(env_keys: list[str], default_path: Path) -> Path:
    for key in env_keys:
        value = os.getenv(key)
        if value:
            path = Path(value).expanduser()
            if path.exists():
                return path
            raise RuntimeError(f"Required path from {key} does not exist: {path}")

    if default_path.exists():
        return default_path
    raise RuntimeError(f"Required path does not exist: {default_path}")


CCTV_FOLDER = resolve_path(
    ["CCTV_RECORDINGS_DIR", "RECORDINGS_DIR"],
    Path("/Volumes/drive/CCTV/recordings/esp_cam1"),
)
NIGHT_EVENTS_FOLDER = resolve_path(
    ["MOTION_DATA_DIR", "DATA_DIR"],
    Path("/Volumes/drive/CCTV/motion/data"),
)
RECORDING_CATALOG = RecordingCatalog(CCTV_FOLDER)
RECORDING_CATALOG.start_background_reconcile()
atexit.register(RECORDING_CATALOG.stop_background_reconcile)

# Create temp folder if it doesn't exist
TEMP_FOLDER.mkdir(parents=True, exist_ok=True)

_generation_guard = threading.Lock()
_generation_locks: dict[Path, tuple[threading.Lock, int]] = {}

app.mount("/static", StaticFiles(directory=STATIC_FOLDER), name="static")


@contextmanager
def generation_lock(path: Path) -> Iterator[None]:
    """Coalesce concurrent requests that generate the same cached media file."""
    canonical = path.resolve()
    with _generation_guard:
        lock, users = _generation_locks.get(canonical, (threading.Lock(), 0))
        _generation_locks[canonical] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _generation_guard:
            current_lock, current_users = _generation_locks[canonical]
            if current_users == 1:
                del _generation_locks[canonical]
            else:
                _generation_locks[canonical] = (current_lock, current_users - 1)


def generate_cached_media(
    output_path: Path,
    generator: Callable[[Path], bool | None],
) -> None:
    """Generate once into a temporary file and publish it atomically."""
    with generation_lock(output_path):
        if output_path.exists() and output_path.stat().st_size > 0:
            return
        temporary = output_path.with_name(
            f".{output_path.stem}.{uuid.uuid4().hex}.tmp{output_path.suffix}"
        )
        try:
            result = generator(temporary)
            if result is False or not temporary.exists() or temporary.stat().st_size == 0:
                raise HTTPException(status_code=500, detail="Failed to generate video")
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)


def redis_command(*parts: str) -> str | None:
    def encode_command(*command_parts: str) -> bytes:
        payload = f"*{len(command_parts)}\r\n"
        for part in command_parts:
            encoded = part.encode("utf-8")
            payload += f"${len(encoded)}\r\n{part}\r\n"
        return payload.encode("utf-8")

    with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=2.0) as client:
        stream = client.makefile("rb")

        if REDIS_PASSWORD:
            client.sendall(encode_command("AUTH", REDIS_PASSWORD))
            response = stream.readline()
            if response.startswith(b"-"):
                raise RuntimeError(response[1:].decode("utf-8", errors="replace").strip())

        if REDIS_DB:
            db = str(REDIS_DB)
            client.sendall(encode_command("SELECT", db))
            response = stream.readline()
            if response.startswith(b"-"):
                raise RuntimeError(response[1:].decode("utf-8", errors="replace").strip())

        client.sendall(encode_command(*parts))
        line = stream.readline()
        if line == b"$-1\r\n":
            return None
        if line.startswith(b"-"):
            raise RuntimeError(line[1:].decode("utf-8", errors="replace").strip())
        if not line.startswith(b"$"):
            raise RuntimeError(f"Unexpected Redis response: {line!r}")

        length = int(line[1:].strip())
        value = stream.read(length)
        stream.read(2)
        return value.decode("utf-8", errors="replace")


def parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_server_info() -> dict:
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except (socket.gaierror, OSError):
        local_ip = "127.0.0.1"
    return {
        "message": "CCTV Video Server",
        "hostname": hostname,
        "local_ip": local_ip,
        "endpoints": {
            "list_videos": "/video/list",
            "last_videos": "/video/last?minutes=5|15|30|60",
            "by_timestamp": "/video/by-timestamp?timestamp=YYYY-MM-DDTHH:MM:SS",
            "by_duration": "/video/by-duration?timestamp=YYYY-MM-DDTHH:MM:SS&minutes=X",
            "by_event_v2": "/video/v2/by-event?start=ISO&end=ISO",
            "by_hour": "/video/by-hour?timestamp=YYYY-MM-DDTHH:MM:SS",
            "by_day": "/video/by-day?timestamp=YYYY-MM-DDTHH:MM:SS",
            "stream_file": "/video/stream/{filename}",
            "motion_logs": "/motion/logs?hours=1|12|24",
            "motion_by_day": "/motion/day?date=YYYY-MM-DD",
            "motion_by_range": "/motion/range?start=ISO&end=ISO",
            "motion_stats": "/motion/stats",
            "motion_hourly_stats": "/motion/stats/hourly?days=30",
            "motion_hourly_stats_last_month": "/motion/stats/hourly-last-month",
            "night_events_list": "/nightevents",
            "night_event_by_index": "/nightevents/{index}",
            "esp32cam_recovery": "/esp32cam/recovery",
            "docs": "/docs",
        },
    }


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_FOLDER / "index.html", media_type="text/html")


@app.get("/api")
def root():
    """Server information and available endpoints."""
    return get_server_info()


@app.get("/api/dashboard")
def dashboard_data(hours: int = 24):
    if hours <= 0 or hours > 168:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 168")

    try:
        events = get_motion_events_by_hours(hours)
        recordings = RECORDING_CATALOG.summary()
        latest = recordings.latest
        return {
            "live_stream_url": LIVE_STREAM_URL,
            "generated_at": datetime.now().isoformat(),
            "hours": hours,
            "motion_count": len(events),
            "events": serialize_motion_events(events),
            "recordings_count": recordings.count,
            "latest_recording": (
                {
                    "filename": latest.path.name,
                    "timestamp": latest.start_time.isoformat(),
                    "duration": latest.duration,
                }
                if latest
                else None
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    recordings = RECORDING_CATALOG.summary()
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "recordings_available": recordings.count,
        "latest_recording_at": (
            recordings.latest.start_time.isoformat() if recordings.latest else None
        ),
        "live_stream_url": LIVE_STREAM_URL,
    }


@app.get("/esp32cam/recovery")
def get_esp32cam_recovery():
    try:
        value = redis_command("GET", ESP32CAM_RECOVERY_REDIS_KEY)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {e}")

    return {"recovery": parse_bool(value)}


def find_videos_in_range(start_time: datetime, end_time: datetime) -> list[Path]:
    """Find all videos within a time range."""
    return [recording.path for recording in RECORDING_CATALOG.overlapping(start_time, end_time)]


def merge_videos(video_files: list[Path], output_path: Path) -> bool:
    """Merge multiple video files using ffmpeg."""
    if not video_files:
        return False

    # Create a text file listing all videos
    list_file = output_path.parent / f"{output_path.stem}_list.txt"

    try:
        with open(list_file, "w") as f:
            for video in video_files:
                # Escape single quotes and write in ffmpeg concat format
                f.write(f"file '{str(video)}'\n")

        # Use ffmpeg to concatenate videos
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",  # Copy without re-encoding (faster)
            "-y",  # Overwrite output file
            str(output_path),
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

    for pattern in ("merged_*.mp4", "event_*.mp4"):
        for file in temp_folder.glob(pattern):
            if current_time - file.stat().st_mtime > 3600:  # 1 hour
                try:
                    file.unlink()
                except:
                    pass


def run_ffmpeg(cmd: list[str], detail: str) -> None:
    if cmd and Path(cmd[0]).name == "ffmpeg" and "-loglevel" not in cmd:
        cmd = [cmd[0], "-hide_banner", "-nostats", "-loglevel", "error", *cmd[1:]]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or detail
        raise HTTPException(status_code=500, detail=message[-500:])


def inline_video_response(path: Path, filename: str | None = None) -> FileResponse:
    """Serve MP4 media for browser playback instead of triggering a download."""
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        filename=filename or path.name,
        content_disposition_type="inline",
    )


def trim_video_accurate(
    input_path: Path,
    output_path: Path,
    offset_seconds: float,
    duration_seconds: float,
    video_bps: int = 1_200_000,
) -> None:
    """Trim with re-encoding so clips do not start after the requested motion time."""
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-ss",
        f"{max(0, offset_seconds):.3f}",
        "-t",
        f"{max(0.1, duration_seconds):.3f}",
        "-c:v",
        "h264_videotoolbox",
        "-allow_sw",
        "0",
        "-realtime",
        "0",
        "-b:v",
        str(video_bps),
        "-maxrate",
        str(max(100_000, int(video_bps * 1.25))),
        "-bufsize",
        str(max(200_000, video_bps * 2)),
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-movflags",
        "+faststart",
        "-y",
        str(output_path),
    ]
    run_ffmpeg(cmd, "Failed to trim video")


def trim_concatenated_recordings(
    video_files: list[Path],
    output_path: Path,
    offset_seconds: float,
    duration_seconds: float,
    video_bps: int = 1_200_000,
) -> None:
    """Concat and accurately trim in one FFmpeg process without an intermediate MP4."""
    list_file = output_path.parent / f"{output_path.stem}_list.txt"
    try:
        with open(list_file, "w") as stream:
            for video in video_files:
                escaped = str(video).replace("'", "'\\''")
                stream.write(f"file '{escaped}'\n")
        run_ffmpeg(
            [
                "ffmpeg",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-ss",
                f"{max(0, offset_seconds):.3f}",
                "-t",
                f"{max(0.1, duration_seconds):.3f}",
                "-c:v",
                "h264_videotoolbox",
                "-allow_sw",
                "0",
                "-realtime",
                "0",
                "-b:v",
                str(video_bps),
                "-maxrate",
                str(max(100_000, int(video_bps * 1.25))),
                "-bufsize",
                str(max(200_000, video_bps * 2)),
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                "-y",
                str(output_path),
            ],
            "Failed to concatenate and trim video",
        )
    finally:
        list_file.unlink(missing_ok=True)


def get_event_clip(
    start_dt: datetime,
    end_dt: datetime,
    merged_path: Path,
    pre_seconds: float,
    post_seconds: float,
    max_bytes: int | None = None,
) -> None:
    clip_start = start_dt - timedelta(seconds=max(0, pre_seconds))
    clip_end = end_dt + timedelta(seconds=max(0, post_seconds))
    selected = RECORDING_CATALOG.overlapping(clip_start, clip_end)

    if not selected:
        raise HTTPException(
            status_code=404,
            detail=f"No video overlaps event window {clip_start.isoformat()} to {clip_end.isoformat()}",
        )

    first_file_dt = selected[0].start_time
    ss_offset = max(0, (clip_start - first_file_dt).total_seconds())
    duration = (clip_end - clip_start).total_seconds()
    video_bps = 1_200_000
    if max_bytes is not None:
        video_bps = max(
            80_000,
            min(video_bps, int((max_bytes * 0.92 * 8) / max(0.1, duration))),
        )

    if len(selected) == 1:
        trim_video_accurate(
            selected[0].path, merged_path, ss_offset, duration, video_bps
        )
        return
    trim_concatenated_recordings(
        [item.path for item in selected], merged_path, ss_offset, duration, video_bps
    )


def ensure_merged_video(video_files: list[Path], output_path: Path) -> None:
    generate_cached_media(output_path, lambda temporary: merge_videos(video_files, temporary))


def ensure_event_clip(
    start_dt: datetime,
    end_dt: datetime,
    output_path: Path,
    pre_seconds: float,
    post_seconds: float,
    max_bytes: int | None = None,
) -> None:
    generate_cached_media(
        output_path,
        lambda temporary: get_event_clip(
            start_dt, end_dt, temporary, pre_seconds, post_seconds, max_bytes
        ),
    )


def media_source_tag(video_files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in video_files:
        stat = path.stat()
        digest.update(path.name.encode())
        digest.update(f":{stat.st_mtime_ns}:{stat.st_size};".encode())
    return digest.hexdigest()[:12]


def parse_datetime_param(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def normalize_for_recording_time(value: datetime) -> datetime:
    """Recordings are named with local naive wall-clock time."""
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def find_video_by_timestamp(timestamp: datetime) -> Path:
    """
    Find video file matching the given timestamp.
    Expected filename format: recording_YYYYMMDD_HHMMSS.mp4
    """
    # Format the timestamp to match your filename pattern
    filename = f"recording_{timestamp.strftime('%Y%m%d_%H%M%S')}.mp4"
    filepath = Path(CCTV_FOLDER) / filename

    if not filepath.exists():
        raise HTTPException(
            status_code=404, detail=f"Video not found for timestamp: {timestamp}"
        )

    return filepath


@app.get("/video/by-duration")
async def get_video_by_duration(
    timestamp: str, minutes: int, background_tasks: BackgroundTasks
):
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
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dt = normalize_for_recording_time(dt)

        # Calculate time range
        start_time = dt
        end_time = dt + timedelta(minutes=minutes)

        recordings = await run_in_threadpool(
            RECORDING_CATALOG.range_with_neighbors,
            start_time,
            end_time,
            before=2,
            after=1,
        )
        if not recordings:
            raise HTTPException(
                status_code=404, detail="No videos found in the recordings folder"
            )
        videos_to_merge = [recording.path for recording in recordings]

        # If only one video, return it directly
        if len(videos_to_merge) == 1:
            return FileResponse(
                path=str(videos_to_merge[0]),
                media_type="video/mp4",
                filename=videos_to_merge[0].name,
            )

        # Generate unique filename for merged video
        timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
        merged_filename = f"merged_duration_{minutes}min_{timestamp_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename

        # Check if merged video already exists
        await run_in_threadpool(ensure_merged_video, videos_to_merge, merged_path)

        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)

        return inline_video_response(merged_path, merged_filename)

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-11-05T19:40:00)",
        )


@app.get("/video/v2/by-event")
async def get_video_by_event(
    start: str,
    end: str,
    background_tasks: BackgroundTasks,
    pre_seconds: float = 10,
    post_seconds: float = 10,
    max_bytes: int | None = None,
):
    """
    Get video trimmed around a motion event.
    Defaults to 10 seconds of padding before and after because event timestamps can
    lag the visible motion and stream-copy trimming can miss short events.

    Args:
        start: ISO format start time (e.g., "2025-11-05T01:23:45")
        end:   ISO format end time   (e.g., "2025-11-05T01:25:10")

    Example:
        /video/v2/by-event?start=2025-11-05T01:23:45&end=2025-11-05T01:25:10
    """
    try:
        start_dt = normalize_for_recording_time(parse_datetime_param(start))
        end_dt = normalize_for_recording_time(parse_datetime_param(end))

        if start_dt >= end_dt:
            raise HTTPException(status_code=400, detail="start must be before end")
        if max_bytes is not None and not 1_000_000 <= max_bytes <= 100_000_000:
            raise HTTPException(
                status_code=400,
                detail="max_bytes must be between 1000000 and 100000000",
            )

        # Build a unique cache filename
        tag = hashlib.md5(
            f"{start}_{end}_{pre_seconds}_{post_seconds}_{max_bytes}".encode()
        ).hexdigest()[:10]
        merged_filename = f"event_{tag}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename

        await run_in_threadpool(
            ensure_event_clip,
            start_dt,
            end_dt,
            merged_path,
            pre_seconds,
            post_seconds,
            max_bytes,
        )

        background_tasks.add_task(cleanup_old_merged_videos)

        return inline_video_response(merged_path, merged_filename)

    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-11-05T01:23:45)",
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
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dt = normalize_for_recording_time(dt)

        # Get hour range
        start_time = dt.replace(minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(hours=1) - timedelta(seconds=1)

        # Find videos in this hour
        videos = await run_in_threadpool(find_videos_in_range, start_time, end_time)

        if not videos:
            raise HTTPException(
                status_code=404,
                detail=f"No videos found for hour: {start_time.strftime('%Y-%m-%d %H:00')}",
            )

        # If only one video, return it directly
        if len(videos) == 1:
            return FileResponse(
                path=str(videos[0]), media_type="video/mp4", filename=videos[0].name
            )

        # Generate unique filename for merged video
        hour_str = start_time.strftime("%Y%m%d_%H")
        merged_filename = f"merged_hour_{hour_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename

        # Check if merged video already exists
        await run_in_threadpool(ensure_merged_video, videos, merged_path)

        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)

        return FileResponse(
            path=str(merged_path), media_type="video/mp4", filename=merged_filename
        )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-10-29T10:00:00)",
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
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dt = normalize_for_recording_time(dt)

        # Get day range
        start_time = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=1) - timedelta(seconds=1)

        # Find videos in this day
        videos = await run_in_threadpool(find_videos_in_range, start_time, end_time)

        if not videos:
            raise HTTPException(
                status_code=404,
                detail=f"No videos found for day: {start_time.strftime('%Y-%m-%d')}",
            )

        # If only one video, return it directly
        if len(videos) == 1:
            return FileResponse(
                path=str(videos[0]), media_type="video/mp4", filename=videos[0].name
            )

        # Generate unique filename for merged video
        day_str = start_time.strftime("%Y%m%d")
        merged_filename = f"merged_day_{day_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename

        # Check if merged video already exists
        await run_in_threadpool(ensure_merged_video, videos, merged_path)

        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)

        return FileResponse(
            path=str(merged_path), media_type="video/mp4", filename=merged_filename
        )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-10-29T00:00:00)",
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
        raise HTTPException(status_code=400, detail="Minutes must be 5, 15, 30, or 60")

    try:
        # Calculate time range
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=minutes)

        recordings = await run_in_threadpool(
            RECORDING_CATALOG.overlapping,
            start_time - timedelta(minutes=10),
            end_time,
        )
        videos = [
            (recording.start_time, recording.path)
            for recording in recordings
        ]

        if not videos:
            raise HTTPException(
                status_code=404,
                detail=f"No videos found for the last {minutes} minutes",
            )

        # Sort by timestamp
        videos.sort(key=lambda x: x[0])
        video_files = [v[1] for v in videos]

        # If only one video, return it directly
        if len(video_files) == 1:
            return FileResponse(
                path=str(video_files[0]),
                media_type="video/mp4",
                filename=video_files[0].name,
            )

        # Generate unique filename for merged video
        source_tag = await run_in_threadpool(media_source_tag, video_files)
        merged_filename = f"merged_last_{minutes}min_{source_tag}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename

        await run_in_threadpool(ensure_merged_video, video_files, merged_path)

        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)

        return FileResponse(
            path=str(merged_path), media_type="video/mp4", filename=merged_filename
        )

    except HTTPException:
        raise
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
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except:
            # Try parsing custom format
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        dt = normalize_for_recording_time(dt)

        recordings = await run_in_threadpool(RECORDING_CATALOG.around, dt)
        if not recordings:
            raise HTTPException(
                status_code=404, detail="No videos found in the recordings folder"
            )
        videos_to_merge = [recording.path for recording in recordings]

        # If only one video, return it directly
        if len(videos_to_merge) == 1:
            return FileResponse(
                path=str(videos_to_merge[0]),
                media_type="video/mp4",
                filename=videos_to_merge[0].name,
            )

        # Generate unique filename for merged video
        timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
        merged_filename = f"merged_timestamp_{timestamp_str}.mp4"
        merged_path = Path(TEMP_FOLDER) / merged_filename

        # Check if merged video already exists
        await run_in_threadpool(ensure_merged_video, videos_to_merge, merged_path)

        # Schedule cleanup of old merged videos
        background_tasks.add_task(cleanup_old_merged_videos)

        return FileResponse(
            path=str(merged_path), media_type="video/mp4", filename=merged_filename
        )

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid timestamp format. Use ISO format (2025-10-29T10:53:42) or YYYY-MM-DD HH:MM:SS",
        )


@app.get("/video/stream/{filename}")
def stream_video(filename: str):
    """
    Stream video by exact filename.

    Example:
        /video/stream/recording_20251029_105804.mp4
    """
    filepath = Path(CCTV_FOLDER) / filename

    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    if not filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Only MP4 files are supported")

    return inline_video_response(filepath, filename)


@app.get("/video/list")
def list_videos(limit: int | None = None, offset: int = 0):
    """
    List all available video recordings with their timestamps.
    """
    try:
        if limit is not None and not 1 <= limit <= 5000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be >= 0")
        videos = [
            {
                "filename": recording.path.name,
                "timestamp": recording.start_time.isoformat(),
                "size_mb": round(recording.size / (1024 * 1024), 2),
                "codec": recording.codec,
                "duration": recording.duration,
            }
            for recording in RECORDING_CATALOG.all(
                descending=True,
                limit=limit,
                offset=offset,
            )
        ]
        return {
            "videos": videos,
            "count": len(videos),
            "total": RECORDING_CATALOG.summary().count,
            "offset": offset,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# MOTION DETECTION API ENDPOINTS
# ============================================================================


def serialize_motion_events(events) -> list[dict]:
    annotations = get_motion_annotations([int(event.id) for event in events])
    return [event.to_dict() | annotations.get(int(event.id), {}) for event in events]


@app.get("/motion/logs")
def get_motion_logs(hours: int = 24):
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
            "events": serialize_motion_events(events),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/day")
def get_motion_by_day(date: str):
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
            "events": serialize_motion_events(events),
        }
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Use YYYY-MM-DD (e.g., 2025-10-31)",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/range")
def get_motion_by_range(start: str, end: str):
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
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except:
            start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")

        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except:
            end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")

        if start_dt >= end_dt:
            raise HTTPException(
                status_code=400, detail="Start time must be before end time"
            )

        events = get_motion_events_by_range(start_dt, end_dt)

        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "count": len(events),
            "events": serialize_motion_events(events),
        }
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid timestamp format. Use ISO format (2025-10-31T10:00:00)",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/stats")
def get_motion_stats():
    """
    Get overall motion detection statistics.

    Example:
        /motion/stats
    """
    try:
        return get_motion_counts()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/stats/hourly")
def get_motion_stats_hourly(days: int = 30):
    """
    Get motion event counts by hour-of-day (00-23) for the last N days.

    Example:
        /motion/stats/hourly?days=30
    """
    try:
        if days <= 0:
            raise HTTPException(status_code=400, detail="days must be > 0")

        stats = get_motion_event_stats_per_hour(days)
        return {
            "days": days,
            "count": len(stats),
            "hourly_stats": stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motion/stats/hourly-last-month")
def get_motion_stats_hourly_last_month():
    """
    Get motion event counts for every hourly bucket over the last 30 days.

    Example:
        /motion/stats/hourly-last-month
    """
    try:
        stats = get_motion_event_stats_per_hour_last_month()
        return {
            "count": len(stats),
            "hourly_stats": stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NIGHT EVENTS API ENDPOINTS
# ============================================================================


@app.get("/nightevents")
def get_night_events():
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
            raise HTTPException(status_code=404, detail="Night events folder not found")

        videos = []

        for file in folder.glob("*.mp4"):
            try:
                idx = int(file.stem)
                videos.append(
                    {
                        "index": idx,
                        "filename": file.name,
                        "size_mb": round(file.stat().st_size / (1024 * 1024), 2),
                    }
                )
            except (ValueError, OSError):
                continue

        return {"count": len(videos), "videos": videos}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/nightevents/{index}")
def get_night_event_by_index(index: int):
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
            raise HTTPException(status_code=404, detail="Night events folder not found")

        video_file = folder / f"{index}.mp4"

        if not video_file.exists():
            raise HTTPException(status_code=404, detail=f"Video {index} not found")

        return inline_video_response(video_file)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    # Get local IP address (best-effort; hostname may not resolve on macOS)
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except (socket.gaierror, OSError):
        local_ip = "127.0.0.1"

    print("\n" + "=" * 60)
    print("🎥 CCTV Video Server Starting...")
    print("=" * 60)
    print(f"📍 Local access:   http://127.0.0.1:8000")
    print(f"🌐 Network access: http://{local_ip}:8000")
    print(f"📚 API docs:       http://{local_ip}:8000/docs")
    print("=" * 60 + "\n")

    # Run server accessible on all network interfaces
    uvicorn.run(
        app,
        host="0.0.0.0",  # Listen on all network interfaces
        port=8005,
        log_level="info",
    )
