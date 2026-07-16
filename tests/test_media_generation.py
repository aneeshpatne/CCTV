import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta


TEST_ROOT = Path("/tmp/cctv-server-unit-tests")
for child in ("motion", "recordings", "night"):
    (TEST_ROOT / child).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MOTION_DB_DIR", str(TEST_ROOT / "motion"))
os.environ.setdefault("CCTV_RECORDINGS_DIR", str(TEST_ROOT / "recordings"))
os.environ.setdefault("MOTION_DATA_DIR", str(TEST_ROOT / "night"))

from server import server
from utilities.recording_catalog import Recording


class MediaGenerationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        server.RECORDING_CATALOG.stop_background_reconcile()

    def test_concurrent_generation_is_single_flight_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cached.mp4"
            calls = 0
            calls_lock = threading.Lock()

            def generate(temporary):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.03)
                temporary.write_bytes(b"complete-video")

            threads = [
                threading.Thread(
                    target=server.generate_cached_media,
                    args=(output, generate),
                )
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(calls, 1)
            self.assertEqual(output.read_bytes(), b"complete-video")
            self.assertEqual(list(output.parent.glob(".*.tmp.mp4")), [])

    def test_event_size_target_is_additive_and_default_bitrate_is_unchanged(self):
        start = datetime(2026, 7, 10, 10, 0)
        recording = Recording(
            path=Path("recording.mp4"),
            start_time=start,
            end_time=start + timedelta(minutes=10),
            duration=600,
            codec="hevc",
            size=1,
        )
        with patch.object(server.RECORDING_CATALOG, "overlapping", return_value=[recording]), patch.object(
            server, "trim_video_accurate"
        ) as trim:
            server.get_event_clip(start, start + timedelta(minutes=5), Path("out.mp4"), 0, 0)
            self.assertEqual(trim.call_args.args[-1], 1_200_000)
            server.get_event_clip(
                start,
                start + timedelta(minutes=5),
                Path("out.mp4"),
                0,
                0,
                max_bytes=2_000_000,
            )
            self.assertLess(trim.call_args.args[-1], 1_200_000)


if __name__ == "__main__":
    unittest.main()
