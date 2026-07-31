import unittest

from utilities.motion_activity import MotionActivityGuard
from utilities.warn import NonBlockingBlinker


class FakeDispatcher:
    def __init__(self):
        self.values = []

    def submit(self, brightness):
        self.values.append(brightness)


class MotionActivityTests(unittest.TestCase):
    def test_guard_ignores_single_frame_noise(self):
        guard = MotionActivityGuard(hold_seconds=10)

        first = guard.update(True, 100)
        self.assertFalse(first.active)
        self.assertFalse(first.started)

        second = guard.update(True, 100.1)
        self.assertTrue(second.active)
        self.assertTrue(second.started)

    def test_guard_holds_until_ten_quiet_seconds(self):
        guard = MotionActivityGuard(hold_seconds=10)

        guard.update(True, 99.9)
        started = guard.update(True, 100)
        self.assertTrue(started.active)
        self.assertTrue(started.started)
        self.assertTrue(guard.update(False, 109.9).active)
        expired = guard.update(False, 110)
        self.assertFalse(expired.active)
        self.assertFalse(expired.started)

    def test_guard_extends_without_restarting_episode(self):
        guard = MotionActivityGuard(hold_seconds=10)

        guard.update(True, 99.9)
        self.assertTrue(guard.update(True, 100).started)
        renewed = guard.update(True, 109)
        self.assertTrue(renewed.active)
        self.assertFalse(renewed.started)
        self.assertTrue(guard.update(False, 118.9).active)
        self.assertFalse(guard.update(False, 119).active)
        guard.update(True, 119.0)
        self.assertTrue(guard.update(True, 119.1).started)

    def test_blinker_starts_immediately_and_ignores_duplicate_start(self):
        dispatcher = FakeDispatcher()
        blinker = NonBlockingBlinker(dispatcher=dispatcher)

        self.assertTrue(blinker.start(duration=30, now=100))
        self.assertEqual(dispatcher.values, [10])
        self.assertFalse(blinker.start(duration=30, now=101))
        self.assertEqual(dispatcher.values, [10])

    def test_blinker_runs_quick_double_flash_and_finishes_off(self):
        dispatcher = FakeDispatcher()
        blinker = NonBlockingBlinker(dispatcher=dispatcher)
        blinker.start(duration=30, now=100)

        blinker.update(now=100.21)
        blinker.update(now=100.41)
        blinker.update(now=100.61)
        blinker.update(now=101.61)
        self.assertEqual(dispatcher.values, [10, 0, 10, 0, 10])

        blinker.update(now=130)
        self.assertFalse(blinker.is_active)
        self.assertEqual(dispatcher.values[-1], 0)

        self.assertTrue(blinker.start(duration=30, now=130))
        self.assertEqual(dispatcher.values[-1], 10)


if __name__ == "__main__":
    unittest.main()
