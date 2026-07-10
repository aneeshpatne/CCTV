# Apple Silicon-Native CCTV Performance Overhaul

## Implementation status (2026-07-10)

Implemented on branch `perf/apple-silicon-native-pipeline`. The native worker,
event protocol, indexed recording catalog, cleanup hysteresis, Python fallback,
FastAPI offloading, Discord clip optimization, launchd configuration, tests, and
benchmark harness are complete. A live pre-cadence canary successfully recorded
HEVC, published RTSP, indexed segments, and persisted motion; see
`benchmarks/canary-results.md`.

Disconnect recovery was subsequently implemented and live-tested with a
controlled ESP reboot. Recording and RTSP retained the HUD on a no-signal frame,
the complete startup loop ran once, MJPEG reconnected automatically, and the
resulting segment remained exactly 540 frames over 59.998 seconds.

The legacy post-connect tuning policy is also restored: 20 seconds after each
stable connection, AWB is disabled, exposure level 2 is applied outside the
12pm–6pm IST exclusion window, and AGC is disabled. Live status verification
reported `framesize=12`, `xclk=20`, `awb=0`, `ae_level=2`, and `agc=0`.

The final fixed-9-fps binary is deployed through
`com.aneesh.cctv.orchestrator`, and `benchmarks/native.json` contains the final
process-tree `top` sample. The MJPEG stream is single-consumer, so do not start a
second capture process while the LaunchAgent is active.

## Objectives

- Replace the per-frame Python/OpenCV path with a macOS Swift worker using VideoToolbox, Metal/Core Image, Vision, and Core ML.
- Preserve the HUD, recording filenames/path, RTSP URL, FastAPI routes, motion-event base schema, and Discord gRPC contract.
- Keep the Python pipeline as an automatic operational fallback.
- Reduce aggregate capture CPU by at least 50% and local bytes/minute by at least 30% at equivalent measured quality.

## Native worker

- Parse the ESP32 multipart MJPEG stream with `URLSession` and hardware-decode JPEG into pooled `CVPixelBuffer`s.
- Bound the latest-frame queue to two frames so stale work is dropped rather than increasing latency.
- Detect broad motion with VideoToolbox motion estimation plus an ROI-aware luminance residual, global-change rejection, and temporal persistence.
- Run Apple Vision/Core ML only on motion candidates to label people and animals; sustained unknown indoor motion remains an event. Vehicle inference is intentionally disabled for this indoor camera.
- Render the timestamp, motion state, RSSI, FPS, temperature, no-signal state, and overlap hiding through one cached Core Image/CoreText composition.
- Hardware-encode local HEVC with `VTCompressionSession`, pass compressed samples into `AVAssetWriter`, and atomically finalize 60-second MP4 segments.
- Hardware-encode low-latency H.264 for the unchanged RTSP URL, using FFmpeg only as a copy-mode RTSP muxer.

## Python and storage integration

- Keep Python responsible for ESP startup/recovery, process supervision, disk cleanup, SQLite persistence, and fallback.
- Receive versioned motion, segment, health, and stream-state events from the native worker over a dedicated file descriptor.
- Keep `motion_events_new` unchanged; add an annotation table and a recording catalog.
- Replace repeated recording-directory scans with indexed catalog queries and startup reconciliation.
- Trigger cleanup at 90% but delete in one batch toward 85%, excluding active partial files and recent recordings.
- Move blocking server video work off the async event loop and use `h264_videotoolbox` for HEVC-to-H.264 event clips.
- Stream nightly downloads to disk and skip Discord re-encoding when the server output is already compliant H.264 under the upload limit.

## Compatibility interfaces

- Preserve `/Volumes/drive/CCTV/recordings/esp_cam1`, `recording_YYYYMMDD_HHMMSS.mp4`, and `rtsp://127.0.0.1:8554/esp_cam1_overlay`.
- Preserve all existing FastAPI paths and the `id`, `start_time`, `end_time`, and `duration` motion fields; labels are additive.
- Preserve `proto/discord_webhook.proto`, RPC names, captions, channel configuration, and size enforcement.
- Add `CCTV_PIPELINE_BACKEND`, `CCTV_NATIVE_BINARY`, `CCTV_MODEL_PATH`, `CCTV_TARGET_FPS`, and `CCTV_LOCAL_CODEC` configuration.
- Fall back to Python after three native failures inside five minutes and latch the fallback until orchestrator restart.

## Verification and rollout

- Capture before/after `top` samples for the complete process tree, plus `ffprobe`, frame-drop, latency, and memory metrics.
- Require at least 50% lower median aggregate CPU; stretch target is 20% aggregate CPU, with motion p95 no higher than 40%.
- Require HEVC bytes/minute at least 30% below H.264 while VMAF is within one point and SSIM within 0.005.
- Require 9.0 ± 0.2 fps, 60 ± 1.5 second valid segments, and no exposed partial files after forced termination.
- Replay a reviewed day/night indoor corpus covering quiet scenes, lighting changes, people, and animals; require at least 95% sustained-motion recall, at least 50% fewer false triggers, and median event-boundary error no greater than two seconds.
- Run Swift unit/replay tests, Python protocol/catalog/cleanup tests, HUD golden frames, API tests, and a stub Discord gRPC integration test.
- Pass replay gates, run a one-hour live canary, then make native the launchd default with Python fallback and review a 24-hour soak.

## Baseline recorded on Apple M4

- Python capture: approximately 54% CPU and 171 MB RSS.
- Two FFmpeg encoders: approximately 6% aggregate CPU and 62 MB RSS.
- Representative 60-second H.264 segment: 5.46 MB at approximately 728 kb/s.
- Sampled hotspots: full-frame HUD blending/copies, MOG2, JPEG decode, raw pipe writes, and repeated PIL RGB packing.
