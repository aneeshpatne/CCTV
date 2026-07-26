import unittest
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
            probe_interval_seconds=100, probe_settle_seconds=4
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
            probe_interval_seconds=100, probe_settle_seconds=4
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
            probe_interval_seconds=100, probe_settle_seconds=4
        )
        self.sustained(policy, 0.12, start=0)
        self.assertEqual(policy.observe(0.8, now=110).action, "off")
        self.assertIsNone(self.sustained(policy, 0.5, start=114))
        self.assertFalse(policy.desired_on)
        self.assertIsNone(self.sustained(policy, 0.5, start=130))


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

    def test_motion_double_pulse_restores_the_latched_state(self):
        client = Mock()
        policy = FloodlightPolicy()
        policy.desired_on = True
        controller = FloodlightController(client, policy=policy, sleep=lambda _: None)
        try:
            self.assertTrue(controller.motion_started())
            controller.close()
        finally:
            controller.close()
        self.assertEqual(
            [entry.args[0] for entry in client.set.call_args_list],
            ["off", "on", "off", "on"],
        )


if __name__ == "__main__":
    unittest.main()
