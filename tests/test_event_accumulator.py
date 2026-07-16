import threading
import time
import unittest

from utilities.EventAccumulator import EventAccumulator


class EventAccumulatorTests(unittest.TestCase):
    def test_sustained_motion_uses_one_worker_and_preserves_padding(self):
        saved = []
        completed = threading.Event()

        def on_save(event):
            saved.append(event)
            completed.set()

        accumulator = EventAccumulator(cooldown=0.05, onSave=on_save)
        worker = accumulator._worker
        started = time.time()
        try:
            for _ in range(20):
                accumulator.trigger()
                time.sleep(0.002)
            self.assertIs(accumulator._worker, worker)
            self.assertTrue(completed.wait(0.5))
            self.assertEqual(len(saved), 1)
            self.assertLessEqual(saved[0]["start_time"], started - 14.9)
            self.assertAlmostEqual(
                saved[0]["duration"],
                saved[0]["end_time"] - saved[0]["start_time"],
            )
        finally:
            accumulator.close()


if __name__ == "__main__":
    unittest.main()
