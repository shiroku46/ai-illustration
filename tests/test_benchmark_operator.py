from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "benchmark-operator.ps1"


class BenchmarkOperatorTest(unittest.TestCase):
    def test_coordinator_only_uses_reviewed_repository_stages(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for required in (
            "prepare-art-references.ps1",
            "install-benchmark-models.ps1",
            "ai_illustration.benchmark_run_package",
            "ai_illustration.benchmark_readiness",
            "ai_illustration.benchmark_execute",
            "ai_illustration.benchmark_results",
            "model-benchmark-plan.v001.json",
            "model-install-manifest.v001.json",
            "local\\benchmark-run-package",
            "local\\benchmark-results",
            "local\\benchmark-contact-sheets",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_effectful_stages_are_explicit_and_bounded(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[switch]$Prepare", source)
        self.assertIn("[switch]$InstallModels", source)
        self.assertIn("[switch]$AcknowledgeExactArtifacts", source)
        self.assertIn("[switch]$AcknowledgeAnimaEvaluationOnly", source)
        self.assertIn("[switch]$ExecuteBenchmark", source)
        self.assertIn("[ValidateRange(1, 144)]", source)
        self.assertIn("[int]$MaxRuns = 3", source)
        self.assertIn("[switch]$RetryFailed", source)
        self.assertIn("[switch]$Finalize", source)
        self.assertIn("-InstallModels requires -AcknowledgeExactArtifacts", source)
        self.assertIn("-InstallModels requires -AcknowledgeAnimaEvaluationOnly", source)
        self.assertIn("-ExecuteBenchmark requires -ComfyUIRoot", source)
        self.assertIn('"--max-runs", [string]$MaxRuns', source)
        self.assertIn('"--execute"', source)

    def test_coordinator_does_not_implement_network_process_launch_or_selection(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for prohibited in (
            "Invoke-WebRequest",
            "Invoke-RestMethod",
            "System.Net.WebClient",
            "DownloadFile",
            "Start-Process",
            "Invoke-Expression",
            "iex ",
            "cmd /c",
            "select_model",
            "aesthetic_score",
            "automatic_score",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)
        self.assertIn("automatic_selection = $false", source)

    def test_endpoint_is_explicit_loopback_and_comfyui_is_never_auto_started(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('http://127.0.0.1:8188', source)
        self.assertIn("Endpoint must use an explicit HTTP loopback host", source)
        self.assertNotIn("ComfyUI.exe", source)
        self.assertNotIn("main.py --listen", source)
        self.assertNotIn("Get-Process", source)


if __name__ == "__main__":
    unittest.main()
