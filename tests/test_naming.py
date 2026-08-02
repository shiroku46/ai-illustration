from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_illustration.naming import content_identifier, export_paths, safe_relative_path


class NamingTests(unittest.TestCase):
    def test_content_identifier_is_stable_and_order_independent(self) -> None:
        self.assertEqual(content_identifier("request", {"a": 1, "b": 2}), content_identifier("request", {"b": 2, "a": 1}))

    def test_export_path_is_deterministic_and_collision_resistant(self) -> None:
        first = export_paths(character_id="boke", crop="full", facing="front", pose="standing-neutral", expression="neutral", version="v001", sha256="a" * 64)
        second = export_paths(character_id="boke", crop="full", facing="front", pose="standing-neutral", expression="neutral", version="v001", sha256="b" * 64)
        self.assertNotEqual(first, second)
        self.assertTrue(first[0].endswith(".png"))
        self.assertTrue(first[1].endswith(".json"))

    def test_path_traversal_is_rejected(self) -> None:
        for value in ("../secret", "/absolute", "a/../../b", r"a\\b"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_relative_path(value)
