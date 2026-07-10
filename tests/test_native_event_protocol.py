import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

_TEMP = tempfile.TemporaryDirectory()
os.environ["CCTV_RECORDINGS_DIR"] = _TEMP.name
os.environ["MOTION_DB_DIR"] = _TEMP.name
os.environ["MOTION_DATA_DIR"] = _TEMP.name
os.environ["CCTV_PIPELINE_BACKEND"] = "python"

from image_processing import pipeline_orchestrator as orchestrator


class NativeEventProtocolTests(unittest.TestCase):
    def test_motion_event_is_persisted_and_annotated(self):
        motion = Mock(id=42)
        with patch.object(orchestrator, "log_motion_event", return_value=motion) as log, patch.object(
            orchestrator, "annotate_motion_event"
        ) as annotate:
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "motion.finalized",
                    "payload": {
                        "start_time": 1_000.0,
                        "end_time": 1_030.0,
                        "duration": 30.0,
                        "confidence": 0.8,
                        "labels": [{"name": "person", "confidence": 0.9}],
                        "detector_version": "test-v1",
                    },
                }
            )
        self.assertEqual(log.call_args.kwargs["duration"], 30.0)
        self.assertEqual(annotate.call_args.args[0], 42)
        self.assertIn("person", annotate.call_args.kwargs["labels_json"])

    def test_segment_event_updates_catalog(self):
        segment = Path(_TEMP.name) / "recording_20260710_120000.mp4"
        segment.write_bytes(b"video")
        with patch.object(orchestrator._recording_catalog, "register") as register:
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "segment.finalized",
                    "payload": {
                        "path": str(segment),
                        "start_time": 1_000.0,
                        "end_time": 1_060.0,
                        "duration": 60.0,
                        "codec": "hevc",
                        "size": 5,
                    },
                }
            )
        self.assertEqual(register.call_args.args[0], segment)
        self.assertEqual(register.call_args.kwargs["codec"], "hevc")

    def test_unknown_protocol_version_is_rejected(self):
        with self.assertRaises(ValueError):
            orchestrator.NativeEventReader._handle(
                {"version": 2, "type": "health", "payload": {}}
            )

    def test_health_event_accepts_additive_vfr_metrics(self):
        with self.assertLogs(level="INFO") as captured:
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "health",
                    "payload": {
                        "fps": 9.5,
                        "camera_fps": 10.2,
                        "output_fps": 9.5,
                        "dropped_frames": 1,
                        "encoder_dropped_frames": 0,
                        "processing_latency_ms": 14.2,
                        "motion_score": 0.1,
                        "recording": True,
                        "rtsp": True,
                    },
                }
            )
        message = "\n".join(captured.output)
        self.assertIn("camera=10.20", message)
        self.assertIn("latency_ms=14.2", message)


if __name__ == "__main__":
    unittest.main()
