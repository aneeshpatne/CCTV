# Native canary results

Captured on Apple M4 on 2026-07-10. The baseline raw `top` report is in
`baseline-python.json`.

| Metric | Python baseline | Native canary | Change |
| --- | ---: | ---: | ---: |
| Aggregate CPU median (`top`) | 58.6% | 20.15% | -65.6% |
| Aggregate CPU p95 (`top`) | 61.2% | 21.0% | -65.7% |
| Representative 60-second segment | 5,461,621 bytes | 1,675,607 bytes | -69.3% |
| Local codec | H.264 | HEVC (hardware) | — |
| Copy-only RTSP muxer | No | Yes | Encode moved to VideoToolbox |

The first native live segment was a valid 1024×768 HEVC/yuv420p MP4, 59.95
seconds long at approximately 224 kb/s. The orchestrator remained healthy,
RTSP became ready, finalized segments were indexed, and a native motion event
was persisted. Cleanup deleted toward 85% in one batch and subsequent checks
did not thrash.

The final native report is in `native.json`. A controlled camera reboot also
verified continuous no-signal recording/RTSP, automatic MJPEG reconnection, and
the complete startup sequence. The resulting segment contained exactly 540
frames over 59.998 seconds. Do not run a second MJPEG consumer while the
LaunchAgent is active because the camera stream is single-consumer.
