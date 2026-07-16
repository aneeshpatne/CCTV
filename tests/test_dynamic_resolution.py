import unittest
from unittest.mock import patch

from utilities.dynamic_resolution import DynamicResolutionController
from utilities.esp32cam_client import CameraStatus
from utilities.startup import startup


class DynamicResolutionControllerTests(unittest.TestCase):
    def make_controller(self, **overrides):
        options = {
            "initial_framesize": 12,
            "dim_threshold": 0.25,
            "bright_threshold": 0.35,
            "observation_seconds": 30,
            "window_seconds": 60,
            "cooldown_seconds": 900,
        }
        options.update(overrides)
        return DynamicResolutionController(**options)

    def test_sustained_dim_scene_selects_framesize_11(self):
        controller = self.make_controller()
        self.assertIsNone(controller.observe(0.20, now=0))
        self.assertIsNone(controller.observe(0.22, now=15))
        decision = controller.observe(0.21, now=30)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.framesize, 11)
        self.assertAlmostEqual(decision.average_brightness, 0.21)

    def test_hysteresis_holds_current_resolution_in_middle_band(self):
        controller = self.make_controller()
        controller.observe(0.30, now=0)
        controller.observe(0.32, now=15)
        self.assertIsNone(controller.observe(0.31, now=30))
        self.assertEqual(controller.selected_framesize, 12)

    def test_successful_change_enforces_fifteen_minute_cooldown(self):
        controller = self.make_controller()
        controller.observe(0.20, now=0)
        decision = controller.observe(0.20, now=30)
        controller.complete_change(decision.framesize, success=True, now=30)

        controller.observe(0.60, now=60)
        self.assertIsNone(controller.observe(0.60, now=90))

        controller.observe(0.60, now=930)
        controller.observe(0.60, now=945)
        decision = controller.observe(0.60, now=960)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.framesize, 12)

    def test_failed_change_can_be_retried_without_cooldown(self):
        controller = self.make_controller()
        controller.observe(0.20, now=0)
        decision = controller.observe(0.20, now=30)
        controller.complete_change(decision.framesize, success=False, now=30)
        retry = controller.observe(0.20, now=31)
        self.assertIsNotNone(retry)
        self.assertEqual(retry.framesize, 11)

    def test_disconnect_requires_fresh_observation_window(self):
        controller = self.make_controller()
        controller.observe(0.20, now=0)
        controller.observe(0.20, now=20)
        controller.reset_observations()
        self.assertIsNone(controller.observe(0.20, now=30))
        self.assertIsNone(controller.observe(0.20, now=50))
        self.assertEqual(controller.observe(0.20, now=60).framesize, 11)


class StartupResolutionTests(unittest.TestCase):
    @patch("utilities.startup.time.sleep")
    @patch("utilities.startup.change_clock")
    @patch("utilities.startup.change_quality")
    @patch("utilities.startup.get_camera_status_with_retry")
    def test_startup_can_lower_framesize_to_11(
        self, get_status, change_quality, _change_clock, _sleep
    ):
        get_status.side_effect = [
            CameraStatus(state="camera-online", framesize=12, raw={"framesize": 12}),
            CameraStatus(state="camera-online", framesize=11, raw={"framesize": 11}),
        ]

        startup(target_framesize=11)

        change_quality.assert_called_once_with(11)


if __name__ == "__main__":
    unittest.main()
