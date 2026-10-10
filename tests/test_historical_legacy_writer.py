"""Catch a genuine RC6 save entry writing before refusing future state."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT = Path(__file__).resolve().parents[1]
FROZEN = PROJECT / "tests" / "fixtures" / "legacy_rc6"
PROBE = PROJECT / "tests" / "_legacy_rc6_probe.py"


class HistoricalLegacyWriterTests(unittest.TestCase):
    def test_legacy_rc6_save_entry_rejects_v3_before_write(self):
        # Break caught: moving/retaining lock acquisition before future-schema refusal.
        # Each case runs the real frozen entry in its own synthetic subprocess.
        for method in ("quick_backup", "reconciliation_backup"):
            with self.subTest(method=method):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-X", "utf8", str(PROBE), method, str(FROZEN)],
                    cwd=FROZEN, text=True, encoding="utf-8", capture_output=True, check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, "ISOLATED_HARNESS_ERROR: " + result.stderr)
                evidence = json.loads(result.stdout)
                print("LEGACY_GATE_EVIDENCE=" + json.dumps(evidence, sort_keys=True), flush=True)
                self.assertTrue(evidence["loaded_modules"])
                self.assertFalse(evidence["real_backend_imported"])
                self.assertEqual(evidence["source_calls"], 0)
                self.assertTrue(evidence["refused"], "legacy entry accepted schema 3")
                self.assertTrue(evidence["fixture_contract_verified"])
                self.assertEqual(evidence["error_type"], "ValueError")
                self.assertEqual(evidence["detector_error_type"], "ValueError")
                self.assertIn(
                    {"module": "wechat_exporter/migrations.py", "function": "migrate_mutable_state"},
                    evidence["refusal_trace"],
                )
                self.assertIn(
                    {"module": "wechat_exporter/migrations.py", "function": "detect_schema_version"},
                    evidence["refusal_trace"],
                )
                self.assertEqual(evidence["before"], evidence["after"])
                self.assertEqual(
                    evidence["mutation_count"], 0,
                    "LEGACY_WRITER_SAFETY=FAIL: target-root writes before refusal: "
                    + json.dumps(evidence["mutations"], sort_keys=True),
                )


if __name__ == "__main__":
    unittest.main()
