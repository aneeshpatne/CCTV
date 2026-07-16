import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from motion import motion


class MotionCompressionTests(unittest.TestCase):
    def test_default_target_respects_transport_limit(self):
        self.assertLess(motion.TARGET_FILE_SIZE_BYTES, motion.HARD_LIMIT_BYTES)

    def test_ffprobe_reads_duration_and_codec_in_one_call(self):
        payload = {
            "streams": [{"codec_name": "h264", "pix_fmt": "yuv420p"}],
            "format": {"duration": "12.5"},
        }
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(payload)},
        )()
        with patch("motion.motion.subprocess.run", return_value=completed) as run:
            self.assertEqual(
                motion._ffprobe_video(Path("clip.mp4")),
                (12.5, "h264", "yuv420p"),
            )
        self.assertEqual(run.call_count, 1)

    def test_compliant_download_can_move_without_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "raw.mp4"
            destination = Path(directory) / "final.mp4"
            source.write_bytes(b"h264")
            with patch(
                "motion.motion._ffprobe_video",
                return_value=(1.0, "h264", "yuv420p"),
            ):
                motion.compress_clip_videotoolbox(
                    source,
                    destination,
                    hard_limit_bytes=100,
                    move_compliant=True,
                )
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"h264")


if __name__ == "__main__":
    unittest.main()
