import os
from pathlib import Path
import threading
import time
import unittest

import cv2
import numpy as np


TEST_ROOT = Path("/tmp/cctv-hud-unit-tests")
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MOTION_DB_DIR", str(TEST_ROOT))
os.environ.setdefault("CCTV_RECORDINGS_DIR", str(TEST_ROOT))
os.environ.setdefault("MOTION_DATA_DIR", str(TEST_ROOT))

from image_processing import camera_pipeline as pipeline


def reference_draw_box(frame, x, y, w, h, bg_color, alpha, border_color, accent_color):
    x2, y2 = x + w, y + h
    x, y = max(0, x), max(0, y)
    x2 = min(frame.shape[1] - 1, x2)
    y2 = min(frame.shape[0] - 1, y2)
    shadow = frame.copy()
    shadow_y = min(frame.shape[0] - 1, y + 2)
    shadow_y2 = min(frame.shape[0] - 1, y2 + 2)
    cv2.rectangle(shadow, (x, shadow_y), (x2, shadow_y2), (0, 0, 0), -1)
    cv2.addWeighted(shadow, 0.18, frame, 0.82, 0, frame)
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x2, y2), bg_color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)
    cv2.rectangle(frame, (x, y), (x2, y2), border_color, 1, cv2.LINE_AA)
    cv2.rectangle(frame, (x, y), (x + 3, y2), accent_color, -1)


def reference_put_text(frame, text, x, center_y, color, font_size):
    font = pipeline.get_hud_font(font_size)
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = pipeline.Image.fromarray(rgb_frame)
    draw = pipeline.ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = bbox[3] - bbox[1]
    y = int(center_y - text_h / 2 - bbox[1])
    draw.text((x, y), text, font=font, fill=(color[2], color[1], color[0]))
    frame[:] = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


class HUDRenderingTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        pipeline.acc.close()

    def test_region_box_blending_matches_full_frame_reference(self):
        rng = np.random.default_rng(7)
        reference = rng.integers(0, 256, (240, 320, 3), dtype=np.uint8)
        optimized = reference.copy()
        args = (20, 14, 120, 34, (31, 31, 31), 0.88, (70, 70, 70), (5, 188, 251))
        reference_draw_box(reference, *args)
        pipeline.draw_box(
            optimized,
            *args[:4],
            bg_color=args[4],
            alpha=args[5],
            border_color=args[6],
            accent_color=args[7],
        )
        np.testing.assert_array_equal(optimized, reference)

    def test_frame_writer_keeps_newest_frame_under_backpressure(self):
        original = pipeline._write_frame_to_ffmpeg_sync
        started = threading.Event()
        release = threading.Event()
        received = []

        def slow_write(frame):
            received.append(int(frame[0, 0, 0]))
            if len(received) == 1:
                started.set()
                release.wait(1)
            return True

        pipeline._write_frame_to_ffmpeg_sync = slow_write
        try:
            for value in (1,):
                pipeline.write_frame_to_ffmpeg(
                    np.full((2, 2, 3), value, dtype=np.uint8)
                )
            self.assertTrue(started.wait(1))
            for value in (2, 3):
                pipeline.write_frame_to_ffmpeg(
                    np.full((2, 2, 3), value, dtype=np.uint8)
                )
            release.set()
            deadline = time.monotonic() + 1
            while len(received) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(received, [1, 3])
        finally:
            release.set()
            pipeline.stop_frame_writer()
            pipeline._write_frame_to_ffmpeg_sync = original

    def test_region_text_rendering_matches_full_frame_reference(self):
        if pipeline.Image is None or pipeline.get_hud_font(14) is None:
            self.skipTest("Pillow HUD font is unavailable")
        rng = np.random.default_rng(9)
        reference = rng.integers(0, 256, (120, 420, 3), dtype=np.uint8)
        optimized = reference.copy()
        reference_put_text(reference, "2026-07-16 12:34:56 PM", 18, 31, (232, 234, 237), 14)
        pipeline.put_hud_text(
            optimized,
            "2026-07-16 12:34:56 PM",
            18,
            31,
            (232, 234, 237),
            14,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            1,
        )
        np.testing.assert_array_equal(optimized, reference)


if __name__ == "__main__":
    unittest.main()
