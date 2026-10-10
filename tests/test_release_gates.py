from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))

from audit_qt_distribution import validate_qt_audit
from create_public_source import create_public_source_zip
from release_gate import (
    ALLOWED_STATUSES,
    evaluate_public_release,
    scan_bytes,
    scan_tree,
    summarize_connections,
    validate_clean_machine_result,
    validate_release_evidence,
)
from stage_corresponding_source import (
    SOURCE_ARCHIVES,
    inspect_source_archive,
    verify_source_archive,
)
from release_config import load_release_config


class ReleaseGateTests(unittest.TestCase):
    def test_current_application_and_windows_resource_pass_release_guard(self):
        """Catches a current app/resource mismatch before release staging."""
        project = Path(__file__).resolve().parents[1]
        config = load_release_config(project, project / "packaging" / "windows_version_info.txt")
        self.assertEqual(config["release_name"], "微信时间胶囊-v0.3.1-rc7-win64")

    def test_qt_audit_requires_all_wheel_metadata_and_dynamic_distribution(self):
        """Catches incomplete Qt notices, static bundling, and UPX distribution."""
        good = {
            "distributions": {
                name: {"version": "6.11.2", "license_files": ["LICENSES/LGPL-3.0-only.txt"]}
                for name in ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6")
            },
            "dynamic_distribution": True,
            "upx": False,
            "shipped_qt_files": ["_internal/PySide6/Qt6Core.dll"],
        }
        self.assertEqual(validate_qt_audit(good), [])

        incomplete = json.loads(json.dumps(good))
        incomplete["distributions"].pop("shiboken6")
        incomplete["dynamic_distribution"] = False
        incomplete["upx"] = True
        self.assertEqual(
            validate_qt_audit(incomplete),
            [
                "missing distribution: shiboken6",
                "Qt runtime is not recorded as dynamically distributed",
                "UPX must remain disabled",
            ],
        )

    def test_source_manifest_pins_exact_official_6112_archives(self):
        """Catches substituting latest or a differently-versioned source archive."""
        self.assertEqual(
            [(item.filename, item.sha256) for item in SOURCE_ARCHIVES],
            [
                (
                    "qt-everywhere-src-6.11.2.tar.xz",
                    "6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33",
                ),
                (
                    "pyside-setup-everywhere-src-6.11.2.zip",
                    "c0fdd62b91a1d36d5ee2e1fb71050a32fbc93fcdeef0fdcb41d29afaaf00d9b5",
                ),
            ],
        )
        for item in SOURCE_ARCHIVES:
            self.assertEqual(item.version, "6.11.2")
            self.assertEqual(item.url.split("/", 3)[2], "download.qt.io")

    def test_source_archive_inspection_lists_version_marker_and_verifies_hash(self):
        """Catches unreadable downloads and archives whose contents do not match 6.11.2."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "fixture-6.11.2.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("fixture-6.11.2/README", "synthetic")
            listing = inspect_source_archive(zip_path, "6.11.2")
            self.assertEqual(listing["archive_type"], "zip")
            self.assertTrue(listing["version_marker_found"])
            self.assertEqual(listing["entry_count"], 1)
            self.assertEqual(
                verify_source_archive(zip_path, listing["sha256"], zip_path.stat().st_size)["status"],
                "PASS",
            )

            tar_path = root / "fixture-6.11.2.tar.xz"
            payload = b"synthetic"
            with tarfile.open(tar_path, "w:xz") as archive:
                info = tarfile.TarInfo("fixture-6.11.2/README")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            listing = inspect_source_archive(tar_path, "6.11.2")
            self.assertEqual(listing["archive_type"], "tar.xz")
            self.assertTrue(listing["version_marker_found"])

    def test_privacy_scan_rejects_nested_names_and_utf8_or_utf16_content(self):
        """Catches identities hidden in nested filenames or ordinary text encodings."""
        patterns = ("Private Contact", "internal-user-prefix", "C:\\Users\\TestUser\\")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested" / "Private Contact"
            nested.mkdir(parents=True)
            (nested / "utf8.txt").write_bytes("internal-user-prefix".encode("utf-8"))
            (root / "utf16.txt").write_bytes("C:\\Users\\TestUser\\".encode("utf-16-le"))
            hits = scan_tree(root, privacy_patterns=patterns)
            self.assertEqual({hit["kind"] for hit in hits}, {"path", "content"})
            self.assertTrue(any(hit["encoding"] == "utf-16-le" for hit in hits))

        self.assertTrue(scan_bytes("Private Contact".encode("utf-8"), patterns))
        self.assertTrue(scan_bytes("Private Contact".encode("utf-16-le"), patterns))

    def test_public_source_uses_allowlist_and_rejects_forbidden_artifacts(self):
        """Catches recursively zipping the checkout or admitting a database via allowlist."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("print('synthetic')\n", encoding="utf-8")
            (root / "private.db").write_bytes(b"database")
            destination = root / "source.zip"
            with self.assertRaisesRegex(ValueError, "forbidden"):
                create_public_source_zip(root, destination, ["app.py", "private.db"])
            create_public_source_zip(root, destination, ["app.py"])
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.namelist(), ["app.py"])

    def test_public_source_directory_allowlist_skips_nested_cache_files(self):
        """Catches recursive allowlist expansion admitting generated cache directories."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            cache = app / "__pycache__"
            cache.mkdir(parents=True)
            (app / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (cache / "module.pyc").write_bytes(b"generated cache")
            destination = root / "source.zip"
            create_public_source_zip(root, destination, ["app"])
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.namelist(), ["app/module.py"])

    def test_clean_machine_result_accepts_only_non_private_contract_fields(self):
        """Catches a result collector serializing usernames, paths, or chat data."""
        allowed = {
            "windows_version": "Windows 11 10.0.26100",
            "architecture": "AMD64",
            "python_installed": False,
            "app_started": True,
            "exit_code": 0,
            "dll_error": False,
            "package_version": "0.3.1-rc.6",
            "package_sha256": "a" * 64,
        }
        self.assertEqual(validate_clean_machine_result(allowed), allowed)
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate_clean_machine_result({**allowed, "username": "Private User"})

    def test_clean_machine_collector_emits_only_the_public_contract(self):
        """Catches the PowerShell collector leaking ambient profile or environment fields."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "fixture.zip"
            package.write_bytes(b"synthetic package")
            result = root / "result.json"
            script = Path(__file__).resolve().parents[1] / "packaging" / "clean-machine" / "collect-result.ps1"
            completed = subprocess.run(
                [
                    "pwsh", "-NoProfile", "-File", str(script),
                    "-ZipPath", str(package), "-OutputPath", str(result),
                    "-AppStarted", "1", "-ExitCode", "0", "-DllError", "0",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(result.read_text(encoding="utf-8-sig"))
            self.assertEqual(set(payload), {
                "windows_version", "architecture", "python_installed", "app_started",
                "exit_code", "dll_error", "package_version", "package_sha256",
            })
            validate_clean_machine_result(payload)

    def test_runtime_connections_are_attributed_by_pid_not_by_wechat_presence(self):
        """Catches counting Weixin traffic as application network activity."""
        records = [
            {"pid": 100, "protocol": "TCP", "remote": "203.0.113.1:443"},
            {"pid": 200, "protocol": "TCP", "remote": "198.51.100.2:443"},
            {"pid": 200, "protocol": "UDP", "remote": "0.0.0.0:*"},
        ]
        result = summarize_connections(records, app_pids={100}, weixin_pids={200})
        self.assertEqual(result["app"]["outbound_count"], 1)
        self.assertEqual(result["weixin"]["outbound_count"], 2)
        self.assertEqual(result["unattributed_count"], 0)

    def test_release_decision_fails_closed_on_fail_or_blocked_hard_gate(self):
        """Catches declaring release-ready while a clean-machine or privacy gate is unresolved."""
        self.assertEqual(
            ALLOWED_STATUSES,
            {"PASS", "FAIL", "BLOCKED", "NOT_REQUIRED", "PENDING_POST_UPLOAD"},
        )
        ready, blockers = evaluate_public_release(
            {"automated_suite": "PASS", "clean_machine": "BLOCKED"},
            hard_gates=("automated_suite", "clean_machine"),
        )
        self.assertFalse(ready)
        self.assertEqual(blockers, ["clean_machine"])
        with self.assertRaisesRegex(ValueError, "invalid gate status"):
            evaluate_public_release({"automated_suite": "mostly-pass"}, ("automated_suite",))

    def test_release_evidence_rejects_each_required_artifact_regression(self):
        """Catches license, source, privacy, and tested-version evidence being weakened."""
        expected_sources = {item.filename: item.sha256 for item in SOURCE_ARCHIVES}
        good = {
            "apache_license_matches": True,
            "third_party_license_file_count": 66,
            "source_hashes": expected_sources,
            "binary_privacy_hits": 0,
            "source_privacy_hits": 0,
            "tested_wechat_versions": ["4.1.13.63", "4.1.13.65"],
        }
        self.assertEqual(validate_release_evidence(good, expected_sources), [])
        mutations = (
            ({**good, "apache_license_matches": False}, "Apache-2.0 LICENSE mismatch"),
            ({**good, "third_party_license_file_count": 65}, "third-party license tree is incomplete"),
            ({**good, "source_hashes": {**expected_sources, next(iter(expected_sources)): "0" * 64}}, "corresponding-source hash mismatch"),
            ({**good, "binary_privacy_hits": 1}, "binary privacy hits: 1"),
            ({**good, "source_privacy_hits": 1}, "source privacy hits: 1"),
            ({**good, "tested_wechat_versions": ["4.1.13.63"]}, "tested WeChat version metadata is incomplete"),
        )
        for evidence, expected_issue in mutations:
            with self.subTest(expected_issue=expected_issue):
                self.assertIn(expected_issue, validate_release_evidence(evidence, expected_sources))


if __name__ == "__main__":
    unittest.main()
