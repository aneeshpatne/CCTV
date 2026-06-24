# CCTV

A self-hosted CCTV automation stack for ESP32-CAM devices. It captures the camera stream, overlays health metrics, records segmented video, restreams RTSP, logs motion events, exposes them over a FastAPI service, and delivers nightly summaries with motion clips to a Discord channel via gRPC.

## Highlights

- Continuous ESP32-CAM management with automatic reboot, quality ramp-up, and clock sync via `utilities/startup.py`.
- Real-time computer vision pipeline (`Image Processing/camera_pipeline.py`) with motion detection inside a configurable ROI, overlays for timestamp/RSSI/FPS/temperature, and LED signalling.
- Dual FFmpeg pipelines for segmented recordings and low-latency RTSP restreaming, with hardware-accelerated VideoToolbox encoding on Apple Silicon, plus disk-usage watchdog and pruning (`Image Processing/pipeline_orchestrator.py`).
- Motion-event persistence in SQLite via SQLAlchemy (`utilities/motion_db.py`) powering a FastAPI service at `server/server.py` for searching, merging, and streaming footage.
- Nightly automation (`motion/motion.py`) that fetches motion windows, downloads footage, compresses clips with Apple Silicon's VideoToolbox (`h264_videotoolbox`) to stay under Discord's webhook file limit, and posts summaries + clips to Discord via gRPC.
- Discord webhook gRPC client (`discord_grpc/`) generated from `proto/discord_webhook.proto`; sends text, images, and videos to a channel by name (default: `cctv`) through an external gRPC server at `127.0.0.1:50051`.
- Operator tooling for camera controls, LED brightness, stream health, and RSSI checks under `tools/`.

## Repository Layout

```
Image Processing/   Capture + motion pipeline and orchestrator utilities
motion/             Nightly downloader, VideoToolbox compressor, and Discord sender
server/             FastAPI application that exposes recordings and motion APIs
discord_grpc/       gRPC client for the Discord webhook service (from proto/discord_webhook.proto)
proto/              Protobuf definitions for the Discord webhook gRPC contract
tools/              Camera control utilities (quality, reset, LED, RSSI, etc.)
utilities/          Shared helpers (startup automation, SQLite logging, warnings)
launchd/            launchd plists for the orchestrator and FastAPI server
run_motion.sh       Apple Silicon launcher for the nightly motion digest
```

## Architecture Overview

```
ESP32-CAM MJPEG → camera_pipeline.py → FFmpeg (VideoToolbox) ─┬─ segmented MP4 recordings (recordings/esp_cam1)
                                                               └─ RTSP restream (rtsp://127.0.0.1:8554/esp_cam1_overlay)
                                                                     │
                                     ┌───────────────────────────────┼─ Storage monitor trims oldest footage when full
                                     │                               ├─ SQLite motion log (utilities/motion_db.py)
                                     │                               ├─ FastAPI server (server/server.py, port 8005) for video & motion APIs
                                     │                               └─ Nightly job (motion/motion.py) → Discord gRPC (#cctv)
                                     │
                Discord webhook gRPC server (127.0.0.1:50051) ←── send_text / send_image / send_video
```

## Requirements

- macOS on Apple Silicon with Python 3.12+.
- FFmpeg CLI with `h264_videotoolbox` support (`brew install ffmpeg`).
- OpenCV build with FFMPEG support.
- ESP32-CAM or compatible device serving MJPEG/HTTP control endpoints (defaults assume `192.168.0.13`).
- SQLite (bundled with Python) and write access to `recordings/esp_cam1` (default) or a custom path via `CCTV_RECORDINGS_DIR`.
- Discord webhook gRPC server listening at `127.0.0.1:50051` (a separate launchd job, `com.aneesh.discord-webhook-grpc`).

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

2. **Recording paths** – By default recordings are written to `recordings/esp_cam1` under the repo root. Override with `CCTV_RECORDINGS_DIR` if you use an external mount.

3. **Environment variables** – A `.env` file is optional. Set `OPENAI_API_KEY` if you run the AI daily digest (`motion/night_message.py`).

4. **Discord webhook gRPC** – The nightly digest sends messages and clips to a Discord channel through a gRPC server. Configure with:

   ```
   DISCORD_GRPC_TARGET=127.0.0.1:50051   # gRPC server address
   DISCORD_CHANNEL=cctv                   # target Discord channel name
   DISCORD_USERNAME=                      # optional webhook display name override
   DISCORD_AVATAR_URL=                    # optional webhook avatar override
   ```

   Regenerate the client stubs after editing the proto:

   ```bash
   python -m grpc_tools.protoc -Iproto --python_out=discord_grpc --grpc_python_out=discord_grpc proto/discord_webhook.proto
   ```

5. **Video compression** – `motion/motion.py` compresses clips with Apple Silicon's `h264_videotoolbox` encoder. The target size is 9.5 MB to fit Discord's standard webhook upload limit; see `discord_grpc.DISCORD_FILE_LIMIT_BYTES` to adjust.

## Running the Services

### 1. Camera Capture + Storage Monitor

```bash
source .venv/bin/activate
python "Image Processing/pipeline_orchestrator.py"
```

This launches the capture pipeline, FFmpeg recorders (VideoToolbox-encoded on Apple Silicon), RTSP restream, and the background disk cleanup job. Adjust `DISK_USAGE_THRESHOLD` and `RECORDINGS_DIR` as needed.

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

### 4. Daily AI Digest

```bash
source .venv/bin/activate
python motion/night_message.py
```

Generates yesterday's motion-plot images, asks the OpenAI model for a short summary, and posts both to Discord via gRPC (`send_text` + `send_image`).

## launchd Services (macOS)

Two persistent agents live in `launchd/` and are installed into `~/Library/LaunchAgents/`:

- `com.aneesh.cctv.orchestrator` – runs `image_processing.pipeline_orchestrator` with `KeepAlive` so the capture/record/RTSP pipeline restarts on failure.
- `com.aneesh.cctv.server` – runs `server.server` (FastAPI on `0.0.0.0:8005`) with `KeepAlive`.

Install / reload both:

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aneesh.cctv.orchestrator.plist
launchctl load ~/Library/LaunchAgents/com.aneesh.cctv.server.plist
```

Logs are written to `~/Library/Logs/CCTV/*.log`. Restart a job with:

```bash
launchctl kickstart -k gui/$(id -u)/com.aneesh.cctv.server
```

## Storage & Maintenance

- **Disk pruning** – The orchestrator trims the oldest MP4 segments when usage exceeds `DISK_USAGE_THRESHOLD`.
- **Motion logging** – `camera_pipeline.py` debounces motion events and queues them for insertion into `motion_logs.db`. Review or rotate the database under the configured recordings directory.
- **Health overlays** – Wi-Fi RSSI (`tools/get_rssi.py`) and ESP SoC temperature (`/syshealth`) power on-screen badges. These requests fail gracefully if endpoints are unreachable.

## Development Tips

- The codebase assumes a single camera. To add more, replicate `camera_pipeline.py` with per-camera constants or abstract them into configuration objects.
- When making IP or credential changes, update both the startup helpers and the motion digest scripts to keep all services aligned.
- Enable `SHOW_LOCAL_VIEW` in `camera_pipeline.py` for debugging overlays, but note the added GUI dependency.
- Use the FastAPI docs UI to exercise the API, especially merge endpoints that rely on sequential file naming.
- Regenerate the Discord gRPC client (`discord_grpc/`) whenever the proto changes — see the Configuration section.

## License

Released under the [GNU General Public License v3.0](LICENSE).
