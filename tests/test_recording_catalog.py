from datetime import datetime
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
