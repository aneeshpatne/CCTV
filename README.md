# CCTV

A self-hosted CCTV automation stack for ESP32-CAM devices. It captures the camera stream, overlays health metrics, records segmented video, restreams RTSP, logs motion events, exposes them over a FastAPI service, and delivers nightly summaries through Telegram.

## Highlights

- Continuous ESP32-CAM management with automatic reboot, quality ramp-up, and clock sync via `utilities/startup.py`.
- Real-time computer vision pipeline (`Image Processing/camera_pipeline.py`) with motion detection inside a configurable ROI, overlays for timestamp/RSSI/FPS/memory, and LED signalling.
- Dual FFmpeg pipelines for segmented recordings and low-latency RTSP restreaming, including disk-usage watchdog and pruning (`Image Processing/pipeline_orchestrator.py`).
- Motion-event persistence in SQLite via SQLAlchemy (`utilities/motion_db.py`) powering a FastAPI service at `server/server.py` for searching, merging, and streaming footage.
- Nightly automation (`motion/motion.py`) that fetches motion windows, downloads footage, GPU-compresses clips, and pushes concise Telegram summaries.
- Operator tooling for camera controls, LED brightness, stream health, RSSI checks, and Telegram broadcasting under `tools/` and `telegram/`.

## Repository Layout

```
Image Processing/   Capture + motion pipeline and orchestrator utilities
motion/             Nightly downloader, compressor, and Telegram sender
server/             FastAPI application that exposes recordings and motion APIs
telegram/           Bot helpers for onboarding and manual broadcasts
tools/              Camera control utilities (quality, reset, LED, RSSI, etc.)
utilities/          Shared helpers (startup automation, SQLite logging, warnings)
run_motion.sh       Helper script for cron-style execution of the nightly job
```

## Architecture Overview

```
ESP32-CAM MJPEG → camera_pipeline.py → FFmpeg ─┬─ segmented MP4 recordings (/media/.../esp_cam1)
                                               └─ RTSP restream (rtsp://127.0.0.1:8554/esp_cam1_overlay)
                                                     │
                                                     ├─ Storage monitor trims oldest footage when full
                                                     ├─ SQLite motion log (utilities/motion_db.py)
                                                     ├─ FastAPI server (server/server.py) for video & motion APIs
                                                     └─ Nightly job (motion/motion.py) → Telegram summaries
```

## Requirements

- Linux with Python 3.10+ (tested on Ubuntu 22.04).
- FFmpeg CLI with NVENC support if GPU compression is desired.
- OpenCV build with FFMPEG support.
- ESP32-CAM or compatible device serving MJPEG/HTTP control endpoints (defaults assume `192.168.0.13`).
- SQLite (bundled with Python) and write access to `/media/aneesh/SSD/recordings/esp_cam1` or a custom path.

### Python Dependencies

Install into a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

> Edit `requirements.txt` if you split optional features across environments.

## Configuration

1. **Camera endpoints** – Update IPs in:

   - `Image Processing/camera_pipeline.py`
   - `utilities/startup.py`
   - `tools/*.py`
   - `motion/motion.py`

2. **Recording paths** – Change the constants pointing to `/media/aneesh/SSD/recordings/esp_cam1` to match your storage mount.

3. **Environment variables** – Copy `.env.example` (create if needed) to `.env` and set:

```
BOT_TOKEN=your_telegram_bot_token
```

4. **Telegram whitelist** – `motion/whitelist.json` and `telegram/whitelist.json` hold user IDs. Populate them manually or run `python telegram/message.py` and issue `/start` from Telegram to register IDs.

5. **GPU compression** – `motion/motion.py` defaults to NVENC. Set `USE_GPU = False` if the host lacks an NVIDIA GPU.

## Running the Services

### 1. Camera Capture + Storage Monitor

```bash
source .venv/bin/activate
python "Image Processing/pipeline_orchestrator.py"
```

This launches the capture pipeline, FFmpeg recorders, RTSP restream, and the background disk cleanup job. Adjust `DISK_USAGE_THRESHOLD` and `RECORDINGS_DIR` as needed.

### 2. FastAPI Video Server

```bash
source .venv/bin/activate
python server/server.py
```

The server listens on `0.0.0.0:8005` by default and exposes documentation at `/docs`.

Common API routes:

- `GET /video/list` – Listing of available recordings with timestamps and sizes.
- `GET /video/by-duration?timestamp=YYYY-MM-DDTHH:MM:SS&minutes=30` – Merge a custom window and return a single MP4.
- `GET /video/by-hour`, `GET /video/by-day`, `GET /video/by-timestamp` – Convenience merges.
- `GET /motion/logs?hours=12`, `/motion/range`, `/motion/day`, `/motion/stats` – Motion event queries backed by SQLite.
- `GET /nightevents` and `/nightevents/{index}` – Serve previously generated nightly clips.

### 3. Nightly Motion Digest

To run ad hoc:

```bash
source .venv/bin/activate
python motion/motion.py
```

For scheduled execution (e.g., via cron), use `run_motion.sh`, which activates the virtual environment, runs the script, and logs to `motion/motion.log`.

### 4. Telegram Utilities

- `python telegram/message.py` – Minimal bot that records `/start` users into the whitelist.
- `python telegram/send_message.py` – Broadcast helper that sends messages and optional MP4 clips to whitelisted users.

## Storage & Maintenance

- **Disk pruning** – The orchestrator trims the oldest MP4 segments when usage exceeds `DISK_USAGE_THRESHOLD`.
- **Motion logging** – `camera_pipeline.py` debounces motion events and queues them for insertion into `motion_logs.db`. Review or rotate the database under the configured recordings directory.
- **Health overlays** – Wi-Fi RSSI (`tools/get_rssi.py`) and ESP memory stats (`/syshealth`) power on-screen badges. These requests fail gracefully if endpoints are unreachable.

## Development Tips

- The codebase assumes a single camera. To add more, replicate `camera_pipeline.py` with per-camera constants or abstract them into configuration objects.
- When making IP or credential changes, update both the startup helpers and the motion digest scripts to keep all services aligned.
- Enable `SHOW_LOCAL_VIEW` in `camera_pipeline.py` for debugging overlays, but note the added GUI dependency.
- Use the FastAPI docs UI to exercise the API, especially merge endpoints that rely on sequential file naming.

## License

Released under the [GNU General Public License v3.0](LICENSE).
