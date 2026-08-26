import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

_TEMP = tempfile.TemporaryDirectory()
os.environ["CCTV_RECORDINGS_DIR"] = _TEMP.name
os.environ["MOTION_DB_DIR"] = _TEMP.name
os.environ["MOTION_DATA_DIR"] = _TEMP.name
os.environ["CCTV_PIPELINE_BACKEND"] = "python"

from image_processing import pipeline_orchestrator as orchestrator
from utilities import motion_db_new as motion_db


class FaceIdentityPersistenceTests(unittest.TestCase):
    def test_identity_label_parsing(self):
        labels = [
            {"name": "person", "confidence": 0.9},
            {"name": "p3", "confidence": 0.81},
            "p12",
        ]
        self.assertEqual(
            motion_db.identity_ids_from_labels(labels),
            [(3, 0.81), (12, 0.0)],
        )

    def test_enroll_event_creates_identity_and_embedding(self):
        orchestrator.NativeEventReader._handle(
            {
                "version": 1,
                "type": "face.enrolled",
                "payload": {
                    "id": 3,
                    "confidence": 1.0,
                    "quality": 0.77,
                    "crop_path": "/tmp/p3.jpg",
                    "embedding": [1.0, 0.0, 0.0],
                    "embedder": "vision-featureprint-v1",
                },
            }
        )
        identity = motion_db.get_face_identity(3)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["name"], "p3")
        self.assertEqual(identity["embedder"], "vision-featureprint-v1")
        self.assertEqual(identity["crop_path"], "/tmp/p3.jpg")
        listed = motion_db.list_face_identities()
        self.assertTrue(any(item["id"] == 3 for item in listed))

    def test_motion_finalize_links_identity_to_event(self):
        motion = motion_db.log_motion_event(
            start_time=datetime(2026, 8, 16, 10, 0, 0),
            end_time=datetime(2026, 8, 16, 10, 0, 30),
            duration=30.0,
        )
        with patch.object(orchestrator, "log_motion_event", return_value=motion), patch.object(
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
                        "labels": [
                            {"name": "person", "confidence": 0.9},
                            {"name": "p3", "confidence": 0.84},
                        ],
                        "detector_version": "vt-motion-v3",
                    },
                }
            )
        self.assertIn("p3", annotate.call_args.kwargs["labels_json"])
        events = motion_db.get_motion_events_for_identity(3)
        self.assertTrue(any(event.id == motion.id for event in events))
        identity = motion_db.get_face_identity(3)
        self.assertGreaterEqual(identity["sightings"], 1)

    def test_export_rebuilds_missing_gallery(self):
        motion_db.upsert_face_identity(
            7,
            embedder="vision-featureprint-v1",
            embedding=[0.0, 1.0],
            quality=0.6,
        )
        directory = Path(_TEMP.name) / "faces-export"
        self.assertTrue(motion_db.export_face_gallery(directory))
        payload = json.loads((directory / "gallery.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["embedder"], "vision-featureprint-v1")
        exported_ids = [item["id"] for item in payload["identities"]]
        self.assertIn(7, exported_ids)
        self.assertFalse(motion_db.export_face_gallery(directory))


if __name__ == "__main__":
    unittest.main()
