from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_illustration.models import load_manifest


class ModelTests(unittest.TestCase):
    def test_loads_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "manifest.json")
            path.write_text(json.dumps({"kind": "character-spec", "id": "boke"}))
            manifest = load_manifest(path)
            self.assertEqual(manifest.kind, "character-spec")
            self.assertEqual(manifest.manifest_id, "boke")

    def test_rejects_non_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "manifest.json")
            path.write_text("[]")
            with self.assertRaises(ValueError):
                load_manifest(path)
