import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from utilities.floodlight import (
    FloodlightClient,
    FloodlightController,
    FloodlightPolicy,
)


class FloodlightPolicyTests(unittest.TestCase):
    @staticmethod
    def sustained(policy, brightness, *, start):
        decision = None
        for offset in (0, 5, 10):
            decision = policy.observe(brightness, now=start + offset) or decision
        return decision

    def test_sustained_darkness_latches_on_and_brightness_does_not_turn_it_off(self):
        policy = FloodlightPolicy(probe_interval_seconds=100)
        decision = self.sustained(policy, 0.12, start=0)
        self.assertEqual(decision.action, "on")
        self.assertTrue(policy.desired_on)
        self.assertIsNone(policy.observe(0.9, now=50))
        self.assertTrue(policy.desired_on)

    def test_periodic_probe_turns_off_then_restores_if_ambient_is_dark(self):
        policy = FloodlightPolicy(
            probe_interval_seconds=100,
            probe_settle_seconds=4,
            minimum_on_seconds=0,
            minimum_off_seconds=0,
        )
        self.sustained(policy, 0.12, start=0)
        probe = policy.observe(0.8, now=110)
        self.assertEqual((probe.action, probe.reason), ("off", "ambient_probe"))
        self.assertIsNone(policy.observe(0.1, now=113))
        restore = self.sustained(policy, 0.1, start=114)
        self.assertEqual((restore.action, restore.reason), ("on", "dark"))

    def test_controller_pauses_image_tuning_during_ambient_probe(self):
        client = Mock()
        policy = FloodlightPolicy(
            probe_interval_seconds=100,
            probe_settle_seconds=4,
            minimum_on_seconds=0,
            minimum_off_seconds=0,
        )
        controller = FloodlightController(client, policy=policy, sleep=lambda _: None)
        try:
            self.sustained(policy, 0.12, start=0)
            self.assertFalse(controller.image_adjustments_paused)
            policy.observe(0.8, now=110)
            self.assertTrue(controller.image_adjustments_paused)
            self.sustained(policy, 0.5, start=114)
            self.assertFalse(controller.image_adjustments_paused)
        finally:
            controller.close()

    def test_periodic_probe_stays_off_in_sustained_daylight(self):
        policy = FloodlightPolicy(
            probe_interval_seconds=100,
            probe_settle_seconds=4,
            minimum_on_seconds=0,
            minimum_off_seconds=0,
        )
        self.sustained(policy, 0.12, start=0)
        self.assertEqual(policy.observe(0.8, now=110).action, "off")
        self.assertIsNone(self.sustained(policy, 0.5, start=114))
        self.assertFalse(policy.desired_on)
        self.assertIsNone(self.sustained(policy, 0.5, start=130))

    def test_minimum_on_time_delays_ambient_probe(self):
        policy = FloodlightPolicy(
            probe_interval_seconds=20,
            minimum_on_seconds=60,
        )
        self.sustained(policy, 0.1, start=0)
        self.assertIsNone(policy.observe(0.8, now=50))
        self.assertEqual(policy.observe(0.8, now=70).action, "off")

    def test_minimum_off_time_prevents_immediate_reactivation(self):
        policy = FloodlightPolicy(
            probe_interval_seconds=20,
            probe_settle_seconds=0,
            minimum_on_seconds=0,
            minimum_off_seconds=30,
        )
        self.sustained(policy, 0.1, start=0)
        self.assertEqual(policy.observe(0.8, now=30).action, "off")
        self.assertIsNone(self.sustained(policy, 0.1, start=31))
        self.assertEqual(self.sustained(policy, 0.1, start=61).action, "on")

    def test_night_profile_uses_its_own_thresholds(self):
        hour = [20]
        policy = FloodlightPolicy(
            night_dark_threshold=0.15,
            night_bright_threshold=0.22,
            night_start_hour=19,
            night_end_hour=6,
            wall_clock=lambda: datetime(2026, 7, 27, hour[0]),
            minimum_off_seconds=0,
        )
        self.assertEqual(policy.active_thresholds, (0.15, 0.22))
        self.assertIsNone(self.sustained(policy, 0.16, start=0))
        self.assertEqual(self.sustained(policy, 0.14, start=20).action, "on")

        hour[0] = 10
        self.assertEqual(policy.active_thresholds, (0.18, 0.25))

    def test_switching_profiles_discards_old_observations(self):
        hour = [18]
        policy = FloodlightPolicy(
            night_dark_threshold=0.15,
            night_bright_threshold=0.22,
            wall_clock=lambda: datetime(2026, 7, 27, hour[0]),
        )
        self.assertIsNone(policy.observe(0.16, now=0))
        self.assertIsNone(policy.observe(0.16, now=5))
        hour[0] = 19
        self.assertIsNone(policy.observe(0.16, now=10))


class FloodlightClientTests(unittest.TestCase):
    def test_json_command_uses_documented_route_and_content_type(self):
        session = Mock()
        response = session.post.return_value
        client = FloodlightClient("http://192.168.1.10/", session=session)
        client.set("on")
        session.post.assert_called_once_with(
            "http://192.168.1.10/api/lights/floodlight",
            json={"action": "on"},
            timeout=1.0,
        )
        response.raise_for_status.assert_called_once_with()

    def test_status_reads_the_named_floodlight_relay(self):
        session = Mock()
        session.get.return_value.json.return_value = {
            "relays": [
                {"name": "bg_light", "on": False},
                {"name": "floodlight", "on": True},
            ]
        }
        client = FloodlightClient("http://192.168.1.10", session=session)
        self.assertTrue(client.get_state())
        session.get.assert_called_once_with(
            "http://192.168.1.10/api/lights/floodlight",
            timeout=1.0,
        )

    def test_device_status_resynchronizes_policy_and_hud_state(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "floodlight.json"
            client = Mock()
            client.get_state.return_value = True
            controller = FloodlightController(client, state_path=state_path)
            try:
                controller._refresh_state()
                self.assertTrue(controller.is_on)
                self.assertTrue(controller.policy.desired_on)
                client.get_state.return_value = False
                controller._refresh_state()
                self.assertFalse(controller.is_on)
                self.assertFalse(controller.policy.desired_on)
            finally:
                controller.close()

    def test_confirmed_state_is_published_for_the_hud(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "floodlight.json"
            client = Mock()
            controller = FloodlightController(
                client, state_path=state_path, sleep=lambda _: None
            )
            try:
                self.assertFalse(controller.is_on)
                self.assertTrue(controller._set("on"))
                self.assertTrue(controller.is_on)
                self.assertIn('"on":true', state_path.read_text())
                self.assertTrue(controller._set("off"))
                self.assertFalse(controller.is_on)
                self.assertIn('"on":false', state_path.read_text())
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
