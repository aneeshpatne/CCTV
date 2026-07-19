import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


BASELINE = {"red": 110, "green": 65, "blue": 72}


class ManualWhiteBalanceControllerTests(unittest.TestCase):
    def make_controller(self, directory, **overrides):
        options = {
            "state_store": WhiteBalanceStateStore(Path(directory) / "state.json"),
            "settle_seconds": 0,
            "observation_seconds": 8,
            "window_seconds": 30,
            "failure_cooldown_seconds": 20,
        }
        options.update(overrides)
        controller = ManualWhiteBalanceController(**options)
        controller.initialize(profile(), BASELINE, now=0)
        return controller

    @staticmethod
    def observe_window(controller, red, blue, *, start=0):
        decision = None
        for index in range(5):
            decision = controller.observe(red, blue, now=start + index * 2) or decision
        return decision

    def test_neutral_target_does_not_change_balanced_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            self.assertIsNone(self.observe_window(controller, 1.0, 0.98))
            state = json.loads((Path(directory) / "state.json").read_text())
            self.assertEqual(state["version"], 3)
            self.assertEqual(state["verified"], BASELINE)
            self.assertEqual(state["baseline"], BASELINE)

    def test_only_largest_out_of_band_channel_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            decision = self.observe_window(controller, 0.8, 1.2)
            self.assertIsNotNone(decision)
            self.assertEqual(decision.action, "adjust")
            self.assertEqual((decision.red, decision.green, decision.blue), (116, 65, 72))

    def test_verified_response_becomes_restart_state(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            decision = self.observe_window(controller, 0.8, 0.98)
            controller.complete(profile(decision.red, decision.green, decision.blue), success=True, now=8)

            self.assertIsNone(self.observe_window(controller, 0.82, 0.98, start=10))
            state = json.loads((Path(directory) / "state.json").read_text())
            self.assertEqual(state["verified"], {"red": 116, "green": 65, "blue": 72})
            self.assertIn("STABLE", controller.status_summary())

    def test_unresponsive_trial_rolls_back_and_enters_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            trial = self.observe_window(controller, 0.8, 0.98)
            controller.complete(profile(trial.red, trial.green, trial.blue), success=True, now=8)

            rollback = self.observe_window(controller, 0.8, 0.98, start=10)
            self.assertIsNotNone(rollback)
            self.assertEqual(rollback.action, "rollback")
            self.assertEqual((rollback.red, rollback.green, rollback.blue), (110, 65, 72))
            controller.complete(profile(), success=True, now=18)
            self.assertIn("GUARDED", controller.status_summary())
            self.assertIsNone(controller.observe(0.8, 0.98, now=37))

    def test_missing_chroma_after_trial_requests_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            trial = self.observe_window(controller, 0.8, 0.98)
            controller.complete(profile(trial.red, trial.green, trial.blue), success=True, now=8)

            self.assertIsNone(controller.observe(None, None, now=15.9))
            rollback = controller.observe(None, None, now=16)
            self.assertEqual(rollback.action, "rollback")
            self.assertEqual((rollback.red, rollback.green, rollback.blue), (110, 65, 72))

    def test_trial_value_is_never_persisted_before_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            trial = self.observe_window(controller, 0.8, 0.98)
            controller.complete(profile(trial.red, trial.green, trial.blue), success=True, now=8)
            state = json.loads((Path(directory) / "state.json").read_text())
            self.assertEqual(state["verified"], BASELINE)
            self.assertEqual(state["controllerState"], "verify")

    def test_safety_range_stops_runaway_at_calibrated_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(
                directory, max_step=32, max_deviation_fraction=0.25
            )
            trial = self.observe_window(controller, 0.1, 0.98)
            self.assertEqual(trial.red, 138)  # round(110 * 1.25)
            controller.complete(profile(138, 65, 72), success=True, now=8)
            self.assertIsNone(self.observe_window(controller, 0.2, 0.98, start=10))
            self.assertIsNone(self.observe_window(controller, 0.2, 0.98, start=20))
            self.assertIn("LIMIT", controller.status_summary())

    def test_logged_magenta_and_green_runaways_roll_back_after_one_stale_step(self):
        scenarios = [
            (0.932, 0.871),  # sequence that previously drove blue from 66 to 255
            (1.077, 1.045),  # sequence that later drove red/blue toward 7/65/7
        ]
        for red_ratio, blue_ratio in scenarios:
            with self.subTest(red_ratio=red_ratio, blue_ratio=blue_ratio):
                with tempfile.TemporaryDirectory() as directory:
                    controller = self.make_controller(directory)
                    trial = self.observe_window(controller, red_ratio, blue_ratio)
                    self.assertIsNotNone(trial)
                    self.assertGreaterEqual(trial.red, round(110 * 0.25))
                    self.assertLessEqual(trial.red, round(110 * 1.75))
                    self.assertGreaterEqual(trial.blue, round(72 * 0.25))
                    self.assertLessEqual(trial.blue, round(72 * 1.75))
                    controller.complete(
                        profile(trial.red, trial.green, trial.blue),
                        success=True,
                        now=8,
                    )
                    rollback = self.observe_window(
                        controller, red_ratio, blue_ratio, start=10
                    )
                    self.assertEqual(rollback.action, "rollback")
                    self.assertEqual(
                        (rollback.red, rollback.green, rollback.blue), (110, 65, 72)
                    )

    def test_per_channel_deadband_does_not_pump_neutral_red(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            decision = self.observe_window(controller, 0.967, 1.03)
            self.assertIsNotNone(decision)
            self.assertEqual(decision.red, 110)
            self.assertLess(decision.blue, 72)

    def test_hold_discards_old_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory, settle_seconds=8)
            controller.initialize(profile(), BASELINE, now=0)
            for index in range(4):
                controller.observe(0.8, 0.98, now=8 + index * 2)
            controller.hold(now=15)
            self.assertIsNone(controller.observe(0.8, 0.98, now=23))

    def test_version_three_verified_values_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WhiteBalanceStateStore(Path(directory) / "state.json")
            store.save(
                {
                    "version": 3,
                    "baseline": BASELINE,
                    "verified": {"red": 116, "green": 65, "blue": 66},
                    "controllerState": "stable",
                }
            )
            controller = ManualWhiteBalanceController(state_store=store)
            self.assertEqual(
                controller.saved_white_balance(BASELINE),
                {"red": 116, "green": 65, "blue": 66},
            )

    def test_legacy_poisoned_state_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WhiteBalanceStateStore(Path(directory) / "state.json")
            store.save(
                {
                    "version": 2,
                    "applied": {"red": 8, "green": 65, "blue": 9},
                }
            )
            controller = ManualWhiteBalanceController(state_store=store)
            self.assertIsNone(controller.saved_white_balance(BASELINE))

    def test_out_of_bounds_or_different_baseline_state_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WhiteBalanceStateStore(Path(directory) / "state.json")
            controller = ManualWhiteBalanceController(state_store=store)
            store.save(
                {
                    "version": 3,
                    "baseline": BASELINE,
                    "verified": {"red": 8, "green": 65, "blue": 9},
                }
            )
            self.assertIsNone(controller.saved_white_balance(BASELINE))
            store.save(
                {
                    "version": 3,
                    "baseline": {"red": 100, "green": 65, "blue": 72},
                    "verified": BASELINE,
                }
            )
            self.assertIsNone(controller.saved_white_balance(BASELINE))

    def test_valid_profile_recovers_transient_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            controller.disable("temporary camera restart")
            controller.initialize(profile(), BASELINE, now=10)
            self.assertNotIn("DISABLED", controller.status_summary())

    def test_chroma_targets_and_safety_settings_are_configurable(self):
        with patch.dict(
            os.environ,
            {
                "CCTV_WB_TARGET_RED_OVER_GREEN": "1.01",
                "CCTV_WB_TARGET_BLUE_OVER_GREEN": "0.97",
                "CCTV_WB_MAX_DEVIATION_FRACTION": "0.2",
                "CCTV_WB_MIN_RESPONSE": "0.003",
                "CCTV_WB_FAILURE_COOLDOWN_SECONDS": "120",
            },
        ):
            controller = ManualWhiteBalanceController.from_environment()
        self.assertEqual(controller.target_red_over_green, 1.01)
        self.assertEqual(controller.target_blue_over_green, 0.97)
        self.assertEqual(controller.max_deviation_fraction, 0.2)
        self.assertEqual(controller.min_response, 0.003)
        self.assertEqual(controller.failure_cooldown_seconds, 120)

    def test_hardware_awb_must_be_off(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make_controller(directory)
            automatic = profile()
            automatic["whiteBalance"]["auto"] = True
            with self.assertRaises(ValueError):
                controller.initialize(automatic, BASELINE)


if __name__ == "__main__":
    unittest.main()
