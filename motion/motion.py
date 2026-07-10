"""Nightly motion digest.

Fetches overnight motion events from the FastAPI server, downloads trimmed MP4
clips for each one, compresses them with Apple Silicon's VideoToolbox hardware
encoder (h264_videotoolbox) to stay under Discord's 25 MB webhook upload limit,
and posts a summary + clip to the configured Discord channel via gRPC.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import requests
from dotenv import load_dotenv

load_dotenv()

from discord_grpc import (
    DISCORD_FILE_LIMIT_BYTES,
    send_text,
    send_video,
)

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8005")

# Compression targets. Discord's free webhook upload limit is 25 MB; we aim a
# little below to leave headroom for the multipart envelope.
TARGET_FILE_SIZE_BYTES = 24 * 1024 * 1024
HARD_LIMIT_BYTES = DISCORD_FILE_LIMIT_BYTES

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("motion")
ist = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# VideoToolbox compression
# ---------------------------------------------------------------------------


def _ffprobe_duration_seconds(path: Path) -> float:
    """Best-effort duration probe using ffprobe (falls back to 0)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return max(float(result.stdout.strip().splitlines()[0]), 0.0)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return 0.0


def _ffprobe_video_codec(path: Path) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,pix_fmt",
                "-of", "default=noprint_wrappers=1", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        values = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                values[key] = value
        return values.get("codec_name"), values.get("pix_fmt")
    except (FileNotFoundError, subprocess.SubprocessError):
        return None, None


def _build_vt_cmd(src: Path, dst: Path, bps: int) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-an",
        "-c:v", "h264_videotoolbox",
        "-b:v", f"{bps}",
        "-maxrate:v", f"{bps}",
        "-bufsize:v", f"{max(2 * bps, 1_000_000)}",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(dst),
    ]


def compress_clip_videotoolbox(
    src: Path,
    dst: Path,
    target_bytes: int = TARGET_FILE_SIZE_BYTES,
    hard_limit_bytes: int = HARD_LIMIT_BYTES,
    max_attempts: int = 6,
) -> Path:
    """Compress `src` to `dst` with Apple Silicon's h264_videotoolbox encoder.

    Picks an initial average bitrate sized to fit `target_bytes` over the
    source duration, then iteratively lowers the bitrate until the output fits
    under `hard_limit_bytes` (Discord's 25 MB cap). Falls back to a 640px-wide
    downscale if bitrate reduction alone is not enough.
    """
    codec, pixel_format = _ffprobe_video_codec(src)
    if src.stat().st_size <= hard_limit_bytes and codec == "h264" and pixel_format in {
        "yuv420p", "nv12"
    }:
        shutil.copyfile(src, dst)
        logger.info("[COMPRESS] Reused compliant H.264 clip without re-encoding: %s", dst.name)
        return dst

    duration = _ffprobe_duration_seconds(src) or 1.0
    payload_budget = target_bytes * 0.97  # headroom for muxer overhead
    bps = max(int((payload_budget / duration) * 8), 80_000)

    for attempt in range(max_attempts):
        cmd = _build_vt_cmd(src, dst, bps)
        logger.info(
            "[COMPRESS] attempt %d bps=%d  size_target=%.1fMB",
            attempt + 1, bps, target_bytes / 1024 / 1024,
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            logger.error("[COMPRESS] ffmpeg failed: %s", result.stderr.strip()[-500:])
            raise RuntimeError("ffmpeg VideoToolbox encode failed")

        size = dst.stat().st_size
        logger.info("[COMPRESS] -> %.2f MB", size / 1024 / 1024)
        if size <= hard_limit_bytes:
            break

        new_bps = max(int(bps * (hard_limit_bytes / size) * 0.85), 60_000)
        if new_bps >= bps:
            break
        bps = new_bps

    if dst.stat().st_size > hard_limit_bytes:
        logger.warning("[COMPRESS] Still over limit; applying fallback downscale.")
        cmd = _build_vt_cmd(src, dst, max(bps, 60_000))
        vf_idx = cmd.index("-i") + 2
        cmd.insert(vf_idx, "-vf")
        cmd.insert(vf_idx + 1, "scale=640:-2")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise RuntimeError("ffmpeg VideoToolbox fallback encode failed")

    size = dst.stat().st_size
    logger.info(
        "[COMPRESS] %s -> %.2f MB (limit %.2f MB)",
        dst.name, size / 1024 / 1024, hard_limit_bytes / 1024 / 1024,
    )
    return dst


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_data_dir() -> Path:
    data_dir = (
        os.getenv("DATA_DIR")
        or os.getenv("MOTION_DATA_DIR")
        or "/Volumes/drive/CCTV/motion/data"
    )
    directory = Path(data_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    logger.info("[SETUP] Data directory ready: %s", directory.absolute())
    return directory


def _cleanup_old_clips(directory: Path) -> None:
    logger.info("[CLEANUP] Starting cleanup of old clips")
    deleted = 0
    media_exts = (".mp4", ".mov", ".mkv", ".webm", ".png", ".jpg", ".jpeg")
    if directory.exists() and directory.is_dir():
        for file in directory.iterdir():
            if not file.is_file() or file.suffix.lower() not in media_exts:
                continue
            try:
                logger.info(
                    "[CLEANUP] Deleting %s (%.2f MB)",
                    file.name, file.stat().st_size / 1024 / 1024,
                )
                file.unlink()
                deleted += 1
            except Exception as exc:
                logger.error("[CLEANUP] Failed to delete %s: %s", file, exc)
    logger.info("[CLEANUP] Deleted %d old file(s)", deleted)


def _fetch_overnight_events() -> list[dict]:
    """Fetch events from 00:00 to 07:00 today (IST). Exits the process early for
    the no-events / fetch-error cases."""
    now_ist = datetime.now(ist).date()
    logger.info(
        "[FETCH] Fetching motion events 00:00-07:00 on %s", now_ist,
    )
    try:
        api_url = (
            f"{API_BASE_URL}/motion/range?"
            f"start={now_ist}T00:00:00&end={now_ist}T07:00:00"
        )
        res = requests.get(api_url, timeout=30)
        res.raise_for_status()
        events = res.json().get("events", [])
    except requests.RequestException as exc:
        logger.error("[FETCH] Request failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("[FETCH] Unexpected error: %s", exc)
        sys.exit(1)

    if not events:
        logger.warning("[FETCH] No motion events found")
        send_text(
            "🌙 **Tonight's events**\n"
            f"📅 Date: {now_ist}\n"
            "⏱️ Time window: 00:00–07:00\n"
            "🎯 Total events: 0\n"
            "⏳ Total duration: 0.00 min\n\n"
            "No motion events detected.",
            timeout=30.0,
        )
        sys.exit(0)

    interval_events: list[dict] = []
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
                end_dt = start_dt + timedelta(seconds=float(duration_raw or 0))
            interval_events.append({"start_time": start_dt, "end_time": end_dt})
            logger.info(
                "[FETCH] Motion event %s -> %s (%.0fs)",
                start_dt.time(), end_dt.time(),
                (end_dt - start_dt).total_seconds(),
            )
        except (ValueError, TypeError) as exc:
            logger.warning("[FETCH] Skipping event %s: %s", d, exc)

    if not interval_events:
        logger.warning("[FETCH] No valid interval events found")
        send_text(
            "🌙 **Tonight's events**\n"
            f"📅 Date: {now_ist}\n"
            "⏱️ Time window: 00:00–07:00\n"
            "🎯 Total events: 0\n"
            "⏳ Total duration: 0.00 min\n\n"
            "No motion events detected.",
            timeout=30.0,
        )
        sys.exit(0)

    logger.info("[FETCH] Retrieved %d motion event(s)", len(interval_events))
    return interval_events


def _merge_nearby_events(interval_events: list[dict]) -> list[dict]:
    logger.info("[MERGE] Merging nearby motion events")
    interval_events.sort(key=lambda x: x["start_time"])
    merged: list[dict] = []
    for event in interval_events:
        if not merged:
            merged.append(event.copy())
            continue
        last = merged[-1]
        if event["start_time"] <= last["end_time"] + timedelta(minutes=2):
            if event["end_time"] > last["end_time"]:
                last["end_time"] = event["end_time"]
        else:
            merged.append(event.copy())
    for item in merged:
        logger.info(
            "[MERGE] %s - %.2f min",
            item["start_time"].time(),
            (item["end_time"] - item["start_time"]).total_seconds() / 60,
        )
    logger.info("[MERGE] Total merged motion events: %d", len(merged))
    return merged


def _download_compress_send(motion_events: list[dict], directory: Path) -> tuple[int, int, int, int]:
    logger.info("[DOWNLOAD] Starting video downloads")
    successful = failed = sent = send_failed = 0

    for idx, item in enumerate(motion_events, 1):
        event_start = item["start_time"]
        event_end = item["end_time"]
        try:
            logger.info(
                "[DOWNLOAD] (%d/%d) motion %s -> %s",
                idx, len(motion_events), event_start.time(), event_end.time(),
            )
            video_url = (
                f"{API_BASE_URL}/video/v2/by-event?"
                f"start={event_start.isoformat()}&end={event_end.isoformat()}"
            )
            raw_path = directory / f"{idx}_raw.mp4"
            downloaded = 0
            with requests.get(video_url, timeout=120, stream=True) as res:
                res.raise_for_status()
                with open(raw_path, "wb") as fh:
                    for chunk in res.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
            logger.info(
                "[DOWNLOAD] ✓ Saved: %s (%.2f MB)",
                raw_path.name, downloaded / 1024 / 1024,
            )
            successful += 1

            clip_path = directory / f"{idx}.mp4"
            compress_clip_videotoolbox(raw_path, clip_path)
            raw_path.unlink(missing_ok=True)

            clip_mb = clip_path.stat().st_size / 1024 / 1024
            duration_min = (event_end - event_start).total_seconds() / 60
            caption = (
                f"🎬 **Event {idx}** — {event_start.strftime('%H:%M:%S')} "
                f"({duration_min:.2f} min, {clip_mb:.1f} MB)"
            )
            try:
                resp = send_video(clip_path, content=caption, timeout=300.0)
                if resp.success:
                    logger.info("[DISCORD] ✓ Sent clip %s (%s)", idx, clip_path.name)
                    sent += 1
                else:
                    logger.error("[DISCORD] ✗ clip %s: %s", idx, resp.error)
                    send_failed += 1
            except Exception as exc:
                logger.exception("[DISCORD] ✗ clip %s: %s", idx, exc)
                send_failed += 1
        except requests.RequestException as exc:
            logger.error("[DOWNLOAD] ✗ Failed to fetch video: %s", exc)
            failed += 1
        except Exception as exc:
            logger.error("[DOWNLOAD] ✗ Unexpected error: %s", exc)
            failed += 1

    return successful, failed, sent, send_failed


def _send_summary(motion_events: list[dict]) -> None:
    logger.info("[DISCORD] Sending motion summary")
    now_ist = datetime.now(ist).date()
    total_duration = (
        sum((e["end_time"] - e["start_time"]).total_seconds() for e in motion_events) / 60
    )
    events_str = "\n".join(
        f"{i} — {e['start_time'].strftime('%H:%M:%S')} "
        f"({(e['end_time'] - e['start_time']).total_seconds() / 60:.2f} min)"
        for i, e in enumerate(motion_events, start=1)
    )
    summary = (
        "🌙 **Tonight's events**\n"
        f"📅 Date: {now_ist}\n"
        "⏱️ Time window: 00:00–07:00\n"
        f"🎯 Total events: {len(motion_events)}\n"
        f"⏳ Total duration: {total_duration:.2f} min\n\n"
        f"{events_str}"
    )
    try:
        send_text(summary, timeout=60.0)
    except Exception as exc:
        logger.exception("[DISCORD] ✗ Failed to send summary: %s", exc)


def main() -> None:
    logger.info("=" * 50)
    logger.info("Motion Detection Video Processor Started")
    logger.info("=" * 50)

    directory = _resolve_data_dir()
    _cleanup_old_clips(directory)

    interval_events = _fetch_overnight_events()
    motion_events = _merge_nearby_events(interval_events)

    successful, failed, sent, send_failed = _download_compress_send(motion_events, directory)

    logger.info("=" * 50)
    logger.info(
        "[SUMMARY] Downloads %d ok / %d failed | Discord clips %d sent / %d failed",
        successful, failed, sent, send_failed,
    )
    logger.info("=" * 50)

    _send_summary(motion_events)
    logger.info("Motion Detection Video Processor Complete")


if __name__ == "__main__":
    main()
