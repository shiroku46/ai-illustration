from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "prepare-art-references.ps1"


class PrepareArtReferencesTest(unittest.TestCase):
    def test_script_pins_exact_owner_reference_hashes_and_names(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("楽子.png", source)
        self.assertIn("櫻.png", source)
        self.assertIn(
            "5d5d67ecca13eebfb762b8251ea0bb00481951d79dcd46c9e44986fc2d069e69",
            source,
        )
        self.assertIn(
            "474465adea571e35a1c722fe96e910f75bdf919f43927cb2ba366186ea672303",
            source,
        )
        self.assertIn("Get-FileHash", source)
        self.assertIn("planned-copy", source)
        self.assertIn("present-verified", source)
        self.assertIn("copied-verified", source)

    def test_script_is_dry_run_by_default_and_does_not_overwrite(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[switch]$Execute", source)
        self.assertIn('mode = $(if ($Execute) { "execute" } else { "dry-run" })', source)
        self.assertIn("will not be overwritten", source)
        self.assertIn(".partial-", source)
        self.assertNotIn("-Force", source)
        self.assertNotIn("Invoke-WebRequest", source)
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("Invoke-Expression", source)


if __name__ == "__main__":
    unittest.main()
