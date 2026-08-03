import os
import unittest
from unittest.mock import Mock, patch

import numpy as np
import requests

from utilities.brightness_mode import (
    ManualExposureController,
    clipping_resistant_brightness,
    clipping_resistant_metrics,
)
from utilities.color_profile import CameraColorProfile
from utilities.esp32cam_client import CameraStatus
from utilities.image_control import ImageControlAPIError, ImageControlClient
from utilities.startup import startup


def frozen_profile(shutter=100, gain=32, *, cached=True):
    return {
        "ok": True,
        "cachedForRecovery": cached,
        "exposure": {
            "autoExposure": False,
            "shutterLines": shutter,
            "autoGain": False,
            "gainX16": gain,
            "gainRegister": 0,
        },
        "whiteBalance": {"auto": False, "red": 90, "green": 64, "blue": 80},
    }


class FakeResponse:
    def __init__(self, status, payload, reason=""):
        self.status_code = status
        self._payload = payload
        self.reason = reason

    def json(self):
        return self._payload


class ImageControlClientTests(unittest.TestCase):
    def test_freeze_has_no_request_body(self):
        session = Mock()
        session.put.return_value = FakeResponse(200, frozen_profile())
        client = ImageControlClient("http://camera", session=session)

        result = client.freeze()

        self.assertEqual(result["exposure"]["gainX16"], 32)
        session.put.assert_called_once_with(
            "http://camera/image-control/freeze", timeout=2.0
        )

    def test_update_uses_partial_manual_exposure_patch(self):
        session = Mock()
        session.put.return_value = FakeResponse(200, frozen_profile(125, 32))
        client = ImageControlClient("http://camera", session=session)

        client.update_exposure(125, 33)

        session.put.assert_called_once_with(
            "http://camera/image-control",
            timeout=2.0,
            json={"exposure": {"shutterLines": 125, "gainX16": 33}},
        )

    def test_freeze_exposure_uses_returned_values_without_touching_color(self):
        session = Mock()
        session.put.return_value = FakeResponse(200, frozen_profile(220, 40))
        client = ImageControlClient("http://camera", session=session)
        client.freeze_exposure(frozen_profile(220, 40))
        session.put.assert_called_once_with(
            "http://camera/image-control",
            timeout=2.0,
            json={"exposure": {"shutterLines": 220, "gainX16": 40}},
        )

    def test_freeze_exposure_clamps_automatic_values_to_manual_limits(self):
        session = Mock()
        session.put.return_value = FakeResponse(200, frozen_profile(1247, 31))
        client = ImageControlClient("http://camera", session=session)
        profile = frozen_profile(1300, 31)
        profile["limits"] = {
            "shutterLines": {"min": 1, "max": 1247},
            "gainX16": {"min": 16, "max": 496},
        }
        client.freeze_exposure(profile)
        session.put.assert_called_once_with(
            "http://camera/image-control",
            timeout=2.0,
            json={"exposure": {"shutterLines": 1247, "gainX16": 31}},
        )

    def test_camera_busy_retries_with_backoff(self):
        busy = FakeResponse(
            503,
            {"ok": False, "error": {"code": "camera_busy", "message": "busy"}},
        )
        session = Mock()
        session.get.side_effect = [busy, busy, FakeResponse(200, frozen_profile())]
        sleeps = []

        ImageControlClient("http://camera", session=session, sleep=sleeps.append).get_profile()

        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(session.get.call_count, 3)

    def test_freeze_awb_uses_no_body_and_longer_default_timeout(self):
        session = Mock()
        payload = frozen_profile()
        payload["whiteBalance"] = {
            "auto": False,
            "red": 142,
            "green": 64,
            "blue": 71,
            "awbStable": True,
            "awbFrames": 0,
        }
        session.put.return_value = FakeResponse(200, payload)
        client = ImageControlClient("http://camera", session=session)

        result = client.freeze_awb()

        self.assertEqual(result["whiteBalance"]["red"], 142)
        session.put.assert_called_once_with(
            "http://camera/image-control/awb/freeze", timeout=15.0
        )

    def test_image_stats_parses_roi_medians(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            200,
            {
                "ok": True,
                "sensor": "OV2640",
                "timestampMs": 1,
                "domain": "jpeg",
                "global": {
                    "meanR": 0.2,
                    "meanG": 0.3,
                    "meanB": 0.25,
                    "meanY": 0.28,
                    "clipBlackFrac": 0.0,
                    "clipWhiteFrac": 0.1,
                },
                "histogram": {"bins": 2, "y": [1, 2], "r": [1, 2], "g": [1, 2], "b": [1, 2]},
                "whiteBalance": {"auto": False, "red": 128, "green": 128, "blue": 128},
                "roi": {
                    "normalized": {"x": 0.51, "y": 0.26, "w": 0.19, "h": 0.48},
                    "samples": 100,
                    "meanR": 0.2,
                    "meanG": 0.3,
                    "meanB": 0.25,
                    "medianRg": 0.9,
                    "medianBg": 0.87,
                    "usable": True,
                },
            },
        )
        stats = ImageControlClient("http://camera", session=session).get_image_stats()
        self.assertTrue(stats.roi.usable)
        self.assertAlmostEqual(stats.roi.median_rg, 0.9)
        self.assertAlmostEqual(stats.roi.median_bg, 0.87)
        self.assertAlmostEqual(stats.mean_y, 0.28)

    def test_probe_capabilities_detects_stats_and_awb_fields(self):
        session = Mock()

        def get(url, timeout=2.0):
            if url.endswith("/image-control"):
                profile = frozen_profile()
                profile["whiteBalance"]["awbStable"] = True
                profile["whiteBalance"]["awbFrames"] = 3
                return FakeResponse(200, profile)
            if url.endswith("/image-stats"):
                return FakeResponse(
                    200,
                    {
                        "ok": True,
                        "global": {"meanY": 0.2},
                        "whiteBalance": {},
                        "roi": {
                            "normalized": {"x": 0.5, "y": 0.2, "w": 0.2, "h": 0.4},
                            "samples": 10,
                            "meanR": 0.1,
                            "meanG": 0.1,
                            "meanB": 0.1,
                            "medianRg": 1.0,
                            "medianBg": 1.0,
                            "usable": True,
                        },
                    },
                )
            if url.endswith("/image-stats/roi"):
                return FakeResponse(
                    200, {"ok": True, "normalized": {"x": 0.5, "y": 0.2, "w": 0.2, "h": 0.4}}
                )
            if url.endswith("/raw-stats"):
                return FakeResponse(
                    501, {"ok": False, "error": {"code": "unsupported_sensor", "message": "no"}}
                )
            raise AssertionError(url)

        session.get.side_effect = get
        caps = ImageControlClient("http://camera", session=session).probe_capabilities()
        self.assertTrue(caps.awb_truthful_fields)
        self.assertTrue(caps.image_stats)
        self.assertTrue(caps.image_stats_roi)
        self.assertTrue(caps.awb_freeze)
        self.assertFalse(caps.raw_stats)

    def test_transient_connection_reset_retries_with_backoff(self):
        session = Mock()
        session.get.side_effect = [
            requests.ConnectionError("reset"),
            requests.ConnectionError("reset"),
            FakeResponse(200, frozen_profile()),
        ]
        sleeps = []

        result = ImageControlClient(
            "http://camera", session=session, sleep=sleeps.append
        ).get_profile()

        self.assertFalse(result["exposure"]["autoExposure"])
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_validation_error_is_structured_and_not_retried(self):
        session = Mock()
        session.put.return_value = FakeResponse(
            400,
            {
                "ok": False,
                "error": {
                    "code": "out_of_range",
                    "field": "exposure.shutterLines",
                    "message": "too large",
                },
            },
        )
        with self.assertRaises(ImageControlAPIError) as raised:
            ImageControlClient("http://camera", session=session).update_exposure(9999, 16)
        self.assertEqual(raised.exception.field, "exposure.shutterLines")
        self.assertEqual(session.put.call_count, 1)


class ManualExposureControllerTests(unittest.TestCase):
    def make_controller(self, **overrides):
        options = {
            "observation_seconds": 4,
            "window_seconds": 12,
            "shutter_max": 300,
            "gain_max_x16": 128,
        }
        options.update(overrides)
        controller = ManualExposureController(**options)
        controller.initialize(frozen_profile())
        return controller

    def test_three_dark_samples_increase_shutter_first_by_at_most_50_percent(self):
        controller = self.make_controller()
        self.assertIsNone(controller.observe(0.10, now=0))
        self.assertIsNone(controller.observe(0.10, now=2))
        decision = controller.observe(0.10, now=4)
        self.assertEqual(decision.direction, "dark")
        self.assertEqual(decision.shutter_lines, 150)
        self.assertEqual(decision.gain_x16, 32)

    def test_dark_scene_uses_shutter_after_gain_cap(self):
        controller = self.make_controller()
        controller.initialize(frozen_profile(100, 128))
        controller.observe(0.10, now=0)
        controller.observe(0.10, now=2)
        decision = controller.observe(0.10, now=4)
        self.assertEqual(decision.shutter_lines, 150)
        self.assertEqual(decision.gain_x16, 128)

    def test_sustained_bright_scene_reduces_gain_before_shutter(self):
        controller = self.make_controller()
        self.assertIsNone(controller.observe(0.80, now=0))
        self.assertIsNone(controller.observe(0.80, now=2))
        decision = controller.observe(0.80, now=4)
        self.assertEqual(decision.direction, "bright")
        self.assertEqual(decision.shutter_lines, 100)
        self.assertEqual(decision.gain_x16, 16)

    def test_bright_scene_shortens_shutter_after_gain_floor(self):
        controller = ManualExposureController(
            observation_seconds=4,
            window_seconds=12,
            shutter_max=1200,
            gain_max_x16=128,
        )
        controller.initialize(frozen_profile(1200, 16))
        controller.observe(0.80, now=0)
        controller.observe(0.80, now=2)
        decision = controller.observe(0.80, now=4)
        self.assertEqual(decision.shutter_lines, 600)
        self.assertEqual(decision.gain_x16, 16)

    def test_opposite_brightness_evidence_restarts_persistence_window(self):
        controller = self.make_controller()
        controller.observe(0.10, now=0)
        controller.observe(0.80, now=2)
        self.assertIsNone(controller.observe(0.80, now=4))
        self.assertIsNotNone(controller.observe(0.80, now=6))

    def test_middle_band_holds_and_success_uses_returned_quantized_gain(self):
        controller = self.make_controller()
        controller.observe(0.30, now=0)
        self.assertIsNone(controller.observe(0.30, now=4))
        controller.reset_observations()
        controller.observe(0.10, now=10)
        controller.observe(0.10, now=12)
        decision = controller.observe(0.10, now=14)
        controller.complete(frozen_profile(100, 44), success=True)
        self.assertIn("GAIN 44/16", controller.status_summary())
        self.assertEqual(decision.gain_x16, 32)

    def test_default_policy_has_wider_shutter_and_lower_gain_caps(self):
        controller = ManualExposureController()
        self.assertEqual(controller.shutter_max, 1247)
        self.assertEqual(controller.gain_max_x16, 31)

    def test_initial_profile_is_normalized_inside_caps(self):
        controller = ManualExposureController(shutter_max=300, gain_max_x16=128)
        patch = controller.initialize(frozen_profile(600, 32))
        self.assertEqual(patch, {"shutterLines": 300, "gainX16": 64})

    def test_disconnect_requires_fresh_evidence(self):
        controller = self.make_controller()
        controller.observe(0.10, now=0)
        controller.reset_observations()
        self.assertIsNone(controller.observe(0.10, now=4))
        self.assertIsNone(controller.observe(0.10, now=6))
        self.assertIsNotNone(controller.observe(0.10, now=8))

    def test_invalid_environment_disables_controller_without_crashing(self):
        with patch.dict(os.environ, {"CCTV_MANUAL_SHUTTER_MAX_LINES": "invalid"}):
            controller = ManualExposureController.from_environment()
        self.assertEqual(controller.status_summary(), "EXPOSURE DISABLED")


class ClippingResistantBrightnessTests(unittest.TestCase):
    def test_clipped_pixels_do_not_bias_usable_midtones(self):
        frame = np.array([[[0, 0, 0], [255, 255, 255], [128, 128, 128]]], dtype=np.uint8)
        self.assertAlmostEqual(clipping_resistant_brightness(frame), 128 / 255, places=5)

    def test_fully_clipped_frame_has_directional_fallback(self):
        black = np.zeros((2, 2, 3), dtype=np.uint8)
        white = np.full((2, 2, 3), 255, dtype=np.uint8)
        self.assertEqual(clipping_resistant_brightness(black), 0.0)
        self.assertEqual(clipping_resistant_brightness(white), 1.0)

    def test_chroma_ignores_clipped_pixels(self):
        frame = np.array(
            [[[0, 0, 0], [255, 255, 255], [100, 100, 120], [100, 100, 120]]],
            dtype=np.uint8,
        )
        metrics = clipping_resistant_metrics(frame)
        self.assertAlmostEqual(metrics.red_over_green, 1.2, places=5)
        self.assertAlmostEqual(metrics.blue_over_green, 1.0, places=5)

    def test_chroma_reference_wall_reports_strong_blue_and_red_casts(self):
        blue_frame = np.full((100, 100, 3), [120, 80, 8], dtype=np.uint8)
        red_frame = np.full((100, 100, 3), [60, 80, 168], dtype=np.uint8)
        blue_metrics = clipping_resistant_metrics(blue_frame)
        red_metrics = clipping_resistant_metrics(red_frame)
        self.assertAlmostEqual(blue_metrics.red_over_green, 0.1, places=5)
        self.assertAlmostEqual(blue_metrics.blue_over_green, 1.5, places=5)
        self.assertAlmostEqual(red_metrics.red_over_green, 2.1, places=5)
        self.assertAlmostEqual(red_metrics.blue_over_green, 0.75, places=5)


class CameraColorProfileTests(unittest.TestCase):
    def test_profile_produces_isolated_white_balance_and_saturation_patches(self):
        profile = CameraColorProfile(94, 65, 84, 72, 72, luma_offset=12, contrast_registers=(48, 48, 48, 10))
        self.assertEqual(
            profile.white_balance_patch(),
            {"whiteBalance": {"auto": False, "red": 94, "green": 65, "blue": 84}},
        )
        self.assertEqual(
            profile.saturation_patch(),
            {"color": {"saturation": {"u": 72, "v": 72}}},
        )
        self.assertEqual(
            profile.tone_patch(),
            {"tone": {"lumaOffset": 12, "contrastRegisters": [48, 48, 48, 10]}},
        )


class StartupResolutionTests(unittest.TestCase):
    @patch("utilities.startup.time.sleep")
    @patch("utilities.startup.change_clock")
    @patch("utilities.startup.change_quality")
    @patch("utilities.startup.get_camera_status_with_retry")
    def test_startup_always_restores_framesize_12(
        self, get_status, change_quality, _change_clock, _sleep
    ):
        get_status.side_effect = [
            CameraStatus(state="camera-online", framesize=11, raw={"framesize": 11}),
            CameraStatus(state="camera-online", framesize=12, raw={"framesize": 12}),
        ]
        startup()
        change_quality.assert_called_once_with(12)
        _change_clock.assert_called_once_with(14)


if __name__ == "__main__":
    unittest.main()
