import json
import tempfile
import unittest
from pathlib import Path

from utilities.white_balance_mode import (
    ManualWhiteBalanceController,
    WhiteBalanceStateStore,
)


def profile(red=110, green=65, blue=72):
    return {
        "exposure": {
            "autoExposure": False,
            "shutterLines": 900,
            "autoGain": False,
            "gainX16": 64,
        },
        "whiteBalance": {
            "auto": False,
            "red": red,
            "green": green,
            "blue": blue,
        },
    }


class ManualWhiteBalanceControllerTests(unittest.TestCase):
    def make_controller(self, directory, **overrides):
        options = {
            "state_store": WhiteBalanceStateStore(Path(directory) / "state.json"),
            "settle_seconds": 0,
            "observation_seconds": 8,
            "window_seconds": 30,
        }
        options.update(overrides)
        controller = ManualWhiteBalanceController(**options)
        controller.initialize(profile(), now=0)
        return controller

    def stabilize(self, controller):
        for index in range(5):
            self.assertIsNone(controller.observe(1.0, 1.0, now=index * 2))

    def test_neutral_target_does_not_change_balanced_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            self.stabilize(controller)
            state = json.loads((Path(directory) / "state.json").read_text())
            self.assertEqual(state["version"], 2)
            self.assertEqual(state["applied"], {"red": 110, "green": 65, "blue": 72})

    def test_blue_cast_raises_red_and_lowers_blue_by_bounded_step(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            self.stabilize(controller)
            decision = None
            for index in range(6):
                decision = controller.observe(0.8, 1.2, now=30 + index * 2) or decision
            self.assertIsNotNone(decision)
            self.assertEqual((decision.red, decision.green, decision.blue), (116, 65, 66))

    def test_deadband_does_not_pump_color(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            self.stabilize(controller)
            decisions = [controller.observe(0.98, 1.02, now=30 + index * 2) for index in range(6)]
            self.assertTrue(all(decision is None for decision in decisions))
            self.assertIn("STABLE", controller.status_summary())

    def test_hold_discards_old_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory, settle_seconds=8)
            controller.initialize(profile(), now=0)
            for index in range(10):
                controller.observe(1.0, 1.0, now=8 + index * 2)
            controller.observe(0.8, 1.2, now=30)
            controller.hold(now=31)
            self.assertIsNone(controller.observe(0.8, 1.2, now=39))

    def test_saved_values_restore_authoritative_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WhiteBalanceStateStore(Path(directory) / "state.json")
            store.save(
                {
                    "version": 2,
                    "applied": {"red": 116, "green": 65, "blue": 66},
                }
            )
            controller = ManualWhiteBalanceController(state_store=store)
            self.assertEqual(
                controller.saved_white_balance(), {"red": 116, "green": 65, "blue": 66}
            )

    def test_legacy_poisoned_state_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WhiteBalanceStateStore(Path(directory) / "state.json")
            store.save(
                {
                    "version": 1,
                    "controllerState": "disabled",
                    "applied": {"red": 139, "green": 65, "blue": 69},
                    "reference": {"redOverGreen": 1.0817, "blueOverGreen": 1.0071},
                    "error": "image profile is not manually frozen",
                }
            )
            controller = ManualWhiteBalanceController(state_store=store)
            self.assertIsNone(controller.saved_white_balance())

    def test_valid_profile_recovers_transient_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            controller.disable("temporary camera restart")
            controller.initialize(profile(), now=10)
            self.assertNotIn("DISABLED", controller.status_summary())

    def test_red_cast_requests_less_red(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            decision = None
            for index in range(5):
                decision = controller.observe(1.097, 1.001, now=index * 2) or decision
            self.assertIsNotNone(decision)
            self.assertLess(decision.red, 110)

    def test_green_cast_requests_more_red_and_blue(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            decision = None
            for index in range(5):
                decision = controller.observe(0.930, 0.914, now=index * 2) or decision
            self.assertIsNotNone(decision)
            self.assertGreater(decision.red, 110)
            self.assertGreater(decision.blue, 72)

    def test_hardware_awb_must_be_off(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            automatic = profile()
            automatic["whiteBalance"]["auto"] = True
            with self.assertRaises(ValueError):
                controller.initialize(automatic)


if __name__ == "__main__":
    unittest.main()
