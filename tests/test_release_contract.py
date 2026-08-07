from __future__ import annotations

from pathlib import Path
import unittest

from ai_illustration.release_audit import RELEASE_VERSION, audit_release


class RepositoryReleaseContractTests(unittest.TestCase):
    def test_repository_software_mvp_contract_is_complete_and_non_mutating(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract = root / "release" / "mvp-v1.json"
        before = contract.read_bytes()

        result = audit_release(contract)

        self.assertTrue(result["ok"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["release"]["version"], RELEASE_VERSION)
        self.assertEqual(
            result["release"]["id"],
            "ai-illustration-mvp-release-b3c88c08d4903d4502fe",
        )
        self.assertEqual(result["critical_file_count"], len(result["critical_files"]))
        self.assertGreaterEqual(result["critical_file_count"], 83)
        self.assertEqual(
            [item["path"] for item in result["critical_files"]],
            sorted(item["path"] for item in result["critical_files"]),
        )
        self.assertFalse(result["network_contacted"])
        self.assertFalse(result["external_process_started"])
        self.assertFalse(result["filesystem_mutated"])
        self.assertEqual(contract.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
