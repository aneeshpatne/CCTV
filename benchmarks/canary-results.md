# Native canary results

Captured on Apple M4 on 2026-07-10. The baseline raw `top` report is in
`baseline-python.json`.

| Metric | Python baseline | Native canary | Change |
| --- | ---: | ---: | ---: |
| Aggregate CPU median (`top`) | 58.6% | Pending final privileged sample | — |
| Aggregate CPU p95 (`top`) | 61.2% | Pending final privileged sample | — |
| Representative 60-second segment | 5,461,621 bytes | 1,675,607 bytes | -69.3% |
| Local codec | H.264 | HEVC (hardware) | — |
| Copy-only RTSP muxer | No | Yes | Encode moved to VideoToolbox |

The first native live segment was a valid 1024×768 HEVC/yuv420p MP4, 59.95
seconds long at approximately 224 kb/s. The orchestrator remained healthy,
RTSP became ready, finalized segments were indexed, and a native motion event
was persisted. Cleanup deleted toward 85% in one batch and subsequent checks
did not thrash.

The canary process currently in memory predates the fixed 9 fps output-cadence
patch. The release binary on disk contains that patch and `-fflags +genpts` for
the H.264 copy muxer, but macOS must restart the LaunchAgent before final frame
rate and `top` validation. Run:

```sh
launchctl kickstart -k gui/$(id -u)/com.aneesh.cctv.orchestrator
```

After one segment has finalized, identify the orchestrator PID and capture the
after report:

```sh
python tools/benchmark_pipeline.py ORCHESTRATOR_PID \
  --seconds 60 --interval 2 --output benchmarks/native.json
```

Then verify the newest segment with `ffprobe`; acceptance is 9.0 ± 0.2 fps and
60 ± 1.5 seconds. Do not run a second MJPEG consumer while the LaunchAgent is
active because the camera stream is single-consumer.
