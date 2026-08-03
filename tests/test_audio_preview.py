from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from ai_illustration.audio_preview import (
    AUDIO_PREVIEW_MANIFEST,
    AudioPreviewError,
    _js_bytes,
    _parse_wav,
    build_audio_preview_package,
    check_audio_preview_package,
)
from ai_illustration.naming import canonical_json


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def wav_bytes(*, frames: int = 8000, rate: int = 8000, channels: int = 1, width: int = 2) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"\0" * frames * channels * width)
    return buffer.getvalue()


class AudioPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.preview_root = self.root / "previews"
        self.package_root = self.root / "packages"
        self.audio_root = self.root / "audio"
        self.output_root = self.root / "output"
        for directory in (self.preview_root, self.package_root, self.audio_root):
            directory.mkdir()

        self.asset = b"\x89PNG\r\n\x1a\nasset"
        self.asset_sha = hashlib.sha256(self.asset).hexdigest()
        self.preview_id = "paper-theater-preview-" + "1" * 20
        self.preview_dir = self.preview_root / self.preview_id
        (self.preview_dir / "assets").mkdir(parents=True)
        (self.preview_dir / "assets" / f"{self.asset_sha}.png").write_bytes(self.asset)
        role = {
            "package_id": "variant-export-package-" + "2" * 20,
            "package_manifest_sha256": "a" * 64,
            "variant_set_ref": "variant-set-" + "3" * 20,
            "character_ref": "character-boke",
            "license_status": "reviewing",
            "stage_slot": "left",
        }
        state = {
            "key": "boke.neutral",
            "variant_id": "variant-boke",
            "asset_path": f"assets/{self.asset_sha}.png",
            "png_sha256": self.asset_sha,
        }
        self.preview = {
            "id": self.preview_id,
            "kind": "paper-theater-preview",
            "schema_version": "1.0",
            "scene_plan_ref": "paper-theater-scene-plan-" + "4" * 20,
            "scene_plan_path": "scene.json",
            "scene_plan_sha256": "b" * 64,
            "width": 1280,
            "height": 720,
            "intent": "evaluation",
            "duration_ms": 1000,
            "roles": {"boke": role, "tsukkomi": {**role, "character_ref": "character-tsukkomi", "stage_slot": "right"}},
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "stage_slots": {"boke": "left", "tsukkomi": "right"},
                    "boke": state,
                    "tsukkomi": {**state, "key": "tsukkomi.neutral", "variant_id": "variant-tsukkomi"},
                }
            ],
            "assets": [{"path": f"assets/{self.asset_sha}.png", "sha256": self.asset_sha, "size": len(self.asset), "source_path": "source.png"}],
            "files": [],
        }
        self.preview_manifest = self.preview_dir / "preview-manifest.json"
        self.preview_manifest.write_bytes(canonical_bytes(self.preview))
        self.audio_bytes = wav_bytes()
        (self.audio_root / "voice.wav").write_bytes(self.audio_bytes)

    def _patch_preview(self):
        return patch(
            "ai_illustration.audio_preview.check_preview_package",
            return_value={"ok": True, "preview": self.preview, "file_count": 4, "segment_count": 1},
        )

    def _build(self, **kwargs):
        options = {"offset_ms": 0, "duration_policy": "exact", "audio_license_status": "reviewing"}
        options.update(kwargs)
        return build_audio_preview_package(
            self.preview_manifest,
            self.preview_root,
            self.package_root,
            "voice.wav",
            self.audio_root,
            self.output_root,
            **options,
        )

    def test_deterministic_dry_run_and_audio_clock_player(self) -> None:
        with self._patch_preview():
            first = self._build()
            second = self._build()
        self.assertEqual(first, second)
        self.assertFalse(self.output_root.exists())
        script = _js_bytes().decode("utf-8")
        self.assertIn("audio.currentTime*1000+data.offset_ms", script)
        self.assertIn("audio.play()", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("mediaDevices", script)
        self.assertNotIn("localStorage", script)

    def test_write_is_byte_preserving_idempotent_and_checkable(self) -> None:
        with self._patch_preview():
            first = self._build(write=True)
            second = self._build(write=True)
            manifest = self.output_root / first["audio_preview"]["id"] / AUDIO_PREVIEW_MANIFEST
            checked = check_audio_preview_package(manifest, self.output_root, self.preview_root, self.package_root, self.audio_root)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertTrue(checked["ok"])
        copied = self.output_root / first["audio_preview"]["id"] / first["audio_preview"]["audio"]["path"]
        self.assertEqual(copied.read_bytes(), self.audio_bytes)

    def test_duration_policies_offsets_and_license_rules(self) -> None:
        with self._patch_preview():
            positive = self._build(offset_ms=100, duration_policy="audio-at-least-scene")
            negative = self._build(offset_ms=-100, duration_policy="scene-at-least-audio")
            self.assertEqual(positive["audio_preview"]["synchronized_audio_end_ms"], 1100)
            self.assertEqual(negative["audio_preview"]["synchronized_audio_end_ms"], 900)
            with self.assertRaisesRegex(AudioPreviewError, "DURATION_MISMATCH"):
                self._build(offset_ms=100, duration_policy="exact")

            self.preview["intent"] = "production"
            self.preview_manifest.write_bytes(canonical_bytes(self.preview))
            with self.assertRaisesRegex(AudioPreviewError, "PRODUCTION_AUDIO_LICENSE"):
                self._build(audio_license_status="reviewing")
            approved = self._build(audio_license_status="approved")
            self.assertEqual(approved["audio_preview"]["intent"], "production")

    def test_malformed_compressed_and_tampered_audio_fail_closed(self) -> None:
        (self.audio_root / "voice.wav").write_bytes(b"not-wave")
        with self._patch_preview(), self.assertRaisesRegex(AudioPreviewError, "WAV_HEADER"):
            self._build()

        compressed = bytearray(wav_bytes())
        compressed[20:22] = struct.pack("<H", 6)
        (self.audio_root / "voice.wav").write_bytes(compressed)
        with self._patch_preview(), self.assertRaisesRegex(AudioPreviewError, "WAV_COMPRESSED"):
            self._build()

        (self.audio_root / "voice.wav").write_bytes(self.audio_bytes)
        with self._patch_preview():
            result = self._build(write=True)
            directory = self.output_root / result["audio_preview"]["id"]
            audio_path = directory / result["audio_preview"]["audio"]["path"]
            audio_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(AudioPreviewError, "FILE_MISMATCH"):
                check_audio_preview_package(directory / AUDIO_PREVIEW_MANIFEST, self.output_root, self.preview_root, self.package_root, self.audio_root)

    def test_ieee_float_header_is_supported_without_decoding(self) -> None:
        frames = 80
        fmt = struct.pack("<HHIIHH", 3, 1, 8000, 32000, 4, 32)
        data = b"\0" * frames * 4
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        facts = _parse_wav(payload)
        self.assertEqual(facts["encoding"], "ieee-float")
        self.assertEqual(facts["frame_count"], frames)


if __name__ == "__main__":
    unittest.main()
