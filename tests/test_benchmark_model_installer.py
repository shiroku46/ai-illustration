from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "install-benchmark-models.ps1"
MANIFEST = ROOT / "benchmark" / "model-install-manifest.v001.json"


class BenchmarkModelInstallerTest(unittest.TestCase):
    def test_script_is_dry_run_by_default_and_fail_closed(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[switch]$Execute", source)
        self.assertIn("[switch]$AcknowledgeExactArtifacts", source)
        self.assertIn("[switch]$AcknowledgeAnimaEvaluationOnly", source)
        self.assertIn('mode = $(if ($Execute) { "execute" } else { "dry-run" })', source)
        self.assertIn("-Execute requires -AcknowledgeExactArtifacts", source)
        self.assertIn("-Execute requires -AcknowledgeAnimaEvaluationOnly", source)
        self.assertIn('status = "planned-download"', source)
        self.assertIn('status = "present-verified"', source)
        self.assertIn('status = "downloaded-verified"', source)

    def test_script_pins_manifest_and_verifies_before_atomic_placement(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        raw_sha = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
        self.assertIn(raw_sha, source)
        self.assertIn("Get-FileHash", source)
        self.assertIn("Get-Item", source)
        self.assertIn(".partial-", source)
        self.assertIn("Invoke-WebRequest", source)
        self.assertIn("Move-Item -LiteralPath $partial -Destination $target", source)
        self.assertIn("will not be overwritten", source)
        self.assertNotIn("-Force", source)
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("powershell -Command", source.lower())

    def test_script_uses_only_manifest_urls_and_fixed_destinations(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[System.Uri]$artifact.source_url", source)
        self.assertIn('$source.Scheme -ne "https"', source)
        for destination in (
            "models/checkpoints",
            "models/diffusion_models",
            "models/text_encoders",
            "models/vae",
        ):
            self.assertIn(destination, source)
        self.assertIn("Assert-SafeChildPath", source)
        self.assertIn("Anima must remain evaluation-only", source)

    @unittest.skipUnless(os.name == "nt", "PowerShell dry-run is verified on Windows")
    def test_windows_dry_run_performs_no_download_or_directory_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SCRIPT),
                    "-ComfyUIRoot",
                    str(root),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            output = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertTrue(output["ok"])
            self.assertEqual("dry-run", output["mode"])
            self.assertEqual(5, output["artifact_count"])
            self.assertEqual(
                {"planned-download"},
                {artifact["status"] for artifact in output["artifacts"]},
            )
            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
