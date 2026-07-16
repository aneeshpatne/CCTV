from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from utilities.recording_catalog import RecordingCatalog


class RecordingCatalogTests(unittest.TestCase):
    def test_reconcile_and_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "recording_20260710_100000.mp4"
            second = root / "recording_20260710_100100.mp4"
            first.write_bytes(b"a" * 10)
            second.write_bytes(b"b" * 20)
            catalog = RecordingCatalog(root)

            self.assertEqual(catalog.reconcile(force=True), 2)
            overlapping = catalog.overlapping(
                datetime(2026, 7, 10, 10, 0, 30),
                datetime(2026, 7, 10, 10, 1, 30),
            )
            self.assertEqual(
                [item.path.name for item in overlapping], [first.name, second.name]
            )

    def test_partial_files_are_never_cataloged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "recording_20260710_100000.mp4.partial"
            partial.write_bytes(b"incomplete")
            catalog = RecordingCatalog(root)
            self.assertEqual(catalog.reconcile(force=True), 0)
            self.assertEqual(catalog.all(), [])

    def test_summary_and_indexed_neighbors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for minute in range(5):
                path = root / f"recording_20260710_100{minute}00.mp4"
                path.write_bytes(bytes([minute]))
                paths.append(path)
            catalog = RecordingCatalog(root)
            catalog.reconcile(force=True)

            summary = catalog.summary()
            self.assertEqual(summary.count, 5)
            self.assertEqual(summary.latest.path, paths[-1])
            selected = catalog.range_with_neighbors(
                datetime(2026, 7, 10, 10, 2),
                datetime(2026, 7, 10, 10, 3),
                before=1,
                after=1,
            )
            self.assertEqual([item.path for item in selected], paths[1:5])
            around = catalog.around(datetime(2026, 7, 10, 10, 2, 30))
            self.assertEqual([item.path for item in around], paths[1:4])
            page = catalog.all(descending=True, limit=2, offset=1)
            self.assertEqual([item.path for item in page], [paths[3], paths[2]])

    def test_reads_do_not_trigger_filesystem_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = RecordingCatalog(root)
            late = root / "recording_20260710_100000.mp4"
            late.write_bytes(b"late")

            self.assertEqual(catalog.all(), [])
            self.assertEqual(catalog.summary().count, 0)
            catalog.reconcile(force=True)
            self.assertEqual(len(catalog.all()), 1)

    def test_concurrent_reconcile_scans_filesystem_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recording_20260710_100000.mp4").write_bytes(b"frame")
            catalog = RecordingCatalog(root)
            original_glob = Path.glob
            outer_checks_complete = threading.Event()
            glob_calls = 0
            monotonic_calls = 0
            counter_lock = threading.Lock()

            def controlled_glob(path, pattern):
                nonlocal glob_calls
                glob_calls += 1
                return original_glob(path, pattern)

            def controlled_monotonic():
                nonlocal monotonic_calls
                with counter_lock:
                    monotonic_calls += 1
                    if monotonic_calls == 2:
                        outer_checks_complete.set()
                return 100.0

            catalog._lock.acquire()
            released = False
            try:
                with patch.object(Path, "glob", controlled_glob), patch(
                    "utilities.recording_catalog.time.monotonic", controlled_monotonic
                ):
                    first = threading.Thread(target=catalog.reconcile)
                    second = threading.Thread(target=catalog.reconcile)
                    first.start()
                    second.start()
                    self.assertTrue(outer_checks_complete.wait(1))
                    catalog._lock.release()
                    released = True
                    first.join()
                    second.join()
            finally:
                if not released:
                    catalog._lock.release()
            self.assertEqual(glob_calls, 1)

    def test_summary_is_not_blocked_by_filesystem_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = RecordingCatalog(root)
            original_glob = Path.glob
            scanning = threading.Event()
            release = threading.Event()

            def slow_glob(path, pattern):
                scanning.set()
                release.wait(1)
                return original_glob(path, pattern)

            with patch.object(Path, "glob", slow_glob):
                worker = threading.Thread(
                    target=catalog.reconcile,
                    kwargs={"force": True},
                )
                worker.start()
                self.assertTrue(scanning.wait(1))
                started = time.perf_counter()
                self.assertEqual(catalog.summary().count, 0)
                elapsed = time.perf_counter() - started
                release.set()
                worker.join()
            self.assertLess(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()
