import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call, patch

_TEMP = tempfile.TemporaryDirectory()
os.environ["CCTV_RECORDINGS_DIR"] = _TEMP.name
os.environ["MOTION_DB_DIR"] = _TEMP.name
os.environ["MOTION_DATA_DIR"] = _TEMP.name
os.environ["CCTV_PIPELINE_BACKEND"] = "python"

from image_processing import pipeline_orchestrator as orchestrator
from utilities.brightness_mode import ManualExposureController, ManualExposureDecision
from utilities.color_profile import CameraColorProfile
from utilities.white_balance_mode import WhiteBalanceDecision


def frozen_profile(shutter=100, gain=32):
    return {
        "ok": True,
        "cachedForRecovery": True,
        "exposure": {
            "autoExposure": False,
            "shutterLines": shutter,
            "autoGain": False,
            "gainX16": gain,
            "gainRegister": 8,
        },
        "whiteBalance": {"auto": False, "red": 94, "green": 65, "blue": 84},
        "color": {"saturation": {"u": 72, "v": 72}},
        "tone": {"lumaOffset": 20, "contrastRegisters": [48, 48, 48, 10]},
    }


class ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def is_alive(self):
        return False

    def start(self):
        self.target()


class NativeEventProtocolTests(unittest.TestCase):
    def setUp(self):
        orchestrator._image_control_ready.set()
        # Event tests must not launch the real periodic camera read-back worker.
        orchestrator._wb_drift_last_check = time.monotonic()

    def tearDown(self):
        orchestrator._image_control_ready.clear()

    def test_startup_applies_exposure_white_balance_and_saturation_separately(self):
        controller = ManualExposureController(gain_max_x16=128)
        white_balance_controller = Mock()
        white_balance_controller.saved_white_balance.return_value = None
        white_balance_controller.status_summary.return_value = "WBCTRL HOLD"
        client = Mock()
        automatic = frozen_profile()
        automatic["exposure"]["autoExposure"] = True
        automatic["exposure"]["autoGain"] = True
        automatic["whiteBalance"]["auto"] = True
        manual = frozen_profile()
        colored = {
            **manual,
            "color": {"saturation": {"u": 72, "v": 72}},
            "tone": {"lumaOffset": 12, "contrastRegisters": [48, 48, 48, 10]},
        }
        client.get_profile.side_effect = [automatic, colored]
        client.freeze_exposure.return_value = manual
        client.update_profile.side_effect = [manual, manual, colored]

        with patch.object(orchestrator, "_exposure_controller", controller), patch.object(
            orchestrator, "_white_balance_controller", white_balance_controller
        ), patch.object(
            orchestrator, "_image_control", client
        ), patch.object(
            orchestrator,
            "_color_profile",
            CameraColorProfile(94, 65, 84, 72, 72, luma_offset=12, contrast_registers=(48, 48, 48, 10)),
        ), patch.object(orchestrator, "_color_profile_error", None):
            orchestrator._initialize_manual_exposure()

        client.freeze_exposure.assert_called_once_with(automatic)
        self.assertEqual(
            client.update_profile.call_args_list,
            [
                call(
                    {"whiteBalance": {"auto": False, "red": 94, "green": 65, "blue": 84}}
                ),
                call({"color": {"saturation": {"u": 72, "v": 72}}}),
                call({"tone": {"lumaOffset": 12, "contrastRegisters": [48, 48, 48, 10]}}),
            ],
        )
        self.assertTrue(orchestrator._image_control_ready.is_set())
        white_balance_controller.saved_white_balance.assert_called_once_with(
            {"red": 94, "green": 65, "blue": 84}
        )
        white_balance_controller.initialize.assert_called_once_with(
            colored, {"red": 94, "green": 65, "blue": 84}
        )

    def test_manual_adjustment_uses_partial_image_control_without_reset(self):
        controller = ManualExposureController()
        controller.initialize(frozen_profile())
        client = Mock()
        client.update_exposure.return_value = frozen_profile(125, 32)
        decision = ManualExposureDecision("dark", 0.20, 125, 32)

        with patch.object(orchestrator, "_exposure_controller", controller), patch.object(
            orchestrator, "_image_control", client
        ), patch.object(orchestrator, "_exposure_adjustment_thread", None), patch.object(
            orchestrator.threading, "Thread", ImmediateThread
        ):
            orchestrator._schedule_exposure_adjustment(decision)

        client.update_exposure.assert_called_once_with(125, 32)
        self.assertIn("125L", controller.status_summary())

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
        with patch.object(orchestrator, "_schedule_wb_drift_check"), self.assertLogs(
            level="INFO"
        ) as captured:
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
                        "scene_brightness": 0.42,
                        "recording": True,
                        "rtsp": True,
                    },
                }
            )
        message = "\n".join(captured.output)
        self.assertIn("camera=10.20", message)
        self.assertIn("latency_ms=14.2", message)
        self.assertIn("brightness=42.0%", message)

    def test_image_metrics_event_feeds_exposure_controller(self):
        controller = Mock()
        controller.observe.return_value = None
        with patch.object(orchestrator, "_exposure_controller", controller):
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "image.metrics",
                    "payload": {"scene_brightness": 0.20},
                }
            )
        controller.observe.assert_called_once_with(0.20)

    def test_image_metrics_event_feeds_white_balance_controller(self):
        exposure = Mock()
        exposure.observe.return_value = None
        white_balance = Mock()
        white_balance.observe.return_value = None
        with (
            patch.object(orchestrator, "_exposure_controller", exposure),
            patch.object(orchestrator, "_white_balance_controller", white_balance),
        ):
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "image.metrics",
                    "payload": {
                        "scene_brightness": 0.30,
                        "red_over_green": 0.92,
                        "blue_over_green": 1.08,
                    },
                }
            )
        white_balance.observe.assert_called_once_with(
            0.92, 1.08, scene_brightness=0.30
        )

    def test_motion_started_event_does_not_control_floodlight(self):
        floodlight = Mock()
        with patch.object(orchestrator, "_floodlight", floodlight):
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "motion.started",
                    "payload": {},
                }
            )
        floodlight.motion_started.assert_not_called()

    def test_floodlight_transition_pauses_image_adjustment_loops(self):
        exposure = Mock()
        white_balance = Mock()
        floodlight = Mock()
        floodlight.observe.return_value = True
        with (
            patch.object(orchestrator, "_exposure_controller", exposure),
            patch.object(orchestrator, "_white_balance_controller", white_balance),
            patch.object(orchestrator, "_floodlight", floodlight),
        ):
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "image.metrics",
                    "payload": {"scene_brightness": 0.12},
                }
            )
        floodlight.observe.assert_called_once_with(0.12)
        exposure.reset_observations.assert_called_once_with()
        exposure.observe.assert_not_called()
        white_balance.hold.assert_called_once_with()

    def test_missing_chroma_is_forwarded_to_white_balance_controller(self):
        exposure = Mock()
        exposure.observe.return_value = None
        white_balance = Mock()
        white_balance.observe.return_value = None
        with (
            patch.object(orchestrator, "_exposure_controller", exposure),
            patch.object(orchestrator, "_white_balance_controller", white_balance),
        ):
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "image.metrics",
                    "payload": {"scene_brightness": 0.30},
                }
            )
        white_balance.observe.assert_called_once_with(
            None, None, scene_brightness=0.30
        )

    def test_white_balance_rollback_uses_partial_image_control_patch(self):
        controller = Mock()
        client = Mock()
        returned = frozen_profile()
        client.update_profile.return_value = returned
        decision = WhiteBalanceDecision("rollback", None, None, 94, 65, 84)

        with (
            patch.object(orchestrator, "_white_balance_controller", controller),
            patch.object(orchestrator, "_image_control", client),
            patch.object(orchestrator, "_white_balance_adjustment_thread", None),
            patch.object(orchestrator.threading, "Thread", ImmediateThread),
        ):
            orchestrator._schedule_white_balance_adjustment(decision)

        client.update_profile.assert_called_once_with(
            {
                "whiteBalance": {
                    "auto": False,
                    "red": 94,
                    "green": 65,
                    "blue": 84,
                }
            }
        )
        controller.complete.assert_called_once_with(returned, success=True)

    def test_image_metrics_are_ignored_until_manual_profile_is_verified(self):
        orchestrator._image_control_ready.clear()
        exposure = Mock()
        orchestrator.NativeEventReader._handle(
            {
                "version": 1,
                "type": "image.metrics",
                "payload": {"scene_brightness": 0.20},
            }
        )
        with patch.object(orchestrator, "_exposure_controller", exposure):
            orchestrator.NativeEventReader._handle(
                {
                    "version": 1,
                    "type": "image.metrics",
                    "payload": {"scene_brightness": 0.20},
                }
            )
        exposure.observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
