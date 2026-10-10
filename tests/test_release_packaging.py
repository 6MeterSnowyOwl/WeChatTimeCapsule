from __future__ import annotations

import os
import sys
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = PROJECT / "packaging" / "wechat_time_capsule.spec"
sys.path.insert(0, str(PROJECT / "packaging"))

from audit_qt_distribution import validate_qt_audit


class ReleasePackagingTests(unittest.TestCase):
    def test_build_refuses_existing_rc7_output_before_toolchain_or_staging(self):
        """Removing the early output guard can overwrite a completed release or use a stale toolchain."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packaging = root / "project" / "packaging"
            packaging.mkdir(parents=True)
            script = packaging / "build_windows.ps1"
            shutil.copyfile(PROJECT / "packaging" / "build_windows.ps1", script)
            output = root / "outputs" / "release" / "v0.3.1-rc7"
            output.mkdir(parents=True)
            sentinel = output / "existing.txt"
            sentinel.write_text("preserve this completed release", encoding="utf-8")
            completed = subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(script), "-SkipInstall"],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("release output already exists", completed.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this completed release")

    def execute_spec(self, binaries, *, version="0.3.1-rc.7", resource_version="0.3.1-rc.7"):
        """Run real spec; replace only the native PyInstaller build boundary."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "wechat_exporter"
            package.mkdir()
            (package / "__init__.py").write_text(
                f'__version__ = "{version}"\nDISPLAY_VERSION = "0.3.1 RC7"\n',
                encoding="utf-8",
            )
            resource = root / "version.txt"
            resource.write_text(
                "VSVersionInfo(kids=[StringStruct('ProductVersion', "
                f"'{resource_version}'), StringStruct('FileDescription', "
                "'微信时间胶囊 v0.3.1 RC7'), StringStruct('FileVersion', '0.3.1.0')])",
                encoding="utf-8",
            )
            collected = {}

            def analysis(*args, **kwargs):
                return types.SimpleNamespace(binaries=list(binaries), pure=[], scripts=[], datas=[])

            def collect(*args, **kwargs):
                collected.update(binaries=list(args[1]), name=kwargs["name"])

            environment = {
                "WTC_STAGE_ROOT": str(root),
                "WTC_WORK_ROOT": str(root / "work"),
                "WTC_VERSION_FILE": str(resource),
                "WTC_MANIFEST_FILE": str(root / "manifest.xml"),
            }
            namespace = {
                "SPEC": str(SPEC),
                "Analysis": analysis,
                "PYZ": lambda *args, **kwargs: None,
                "EXE": lambda *args, **kwargs: None,
                "COLLECT": collect,
            }
            uia_spec = types.SimpleNamespace(submodule_search_locations=[str(root / "uiautomation")])
            with patch.dict(os.environ, environment), patch("importlib.util.find_spec", return_value=uia_spec):
                exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), namespace)
            return collected

    def test_spec_excludes_unused_gpl_virtual_keyboard_but_preserves_windows_input(self):
        """Removing the spec filter would ship GPL-only VK DLLs; broad filtering breaks native input."""
        binaries = [
            ("PySide6/Qt6VirtualKeyboard.dll", "C:/qt/Qt6VirtualKeyboard.dll", "BINARY"),
            ("PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll", "C:/qt/qtvirtualkeyboardplugin.dll", "BINARY"),
            ("PySide6/Qt6Core.dll", "C:/qt/Qt6Core.dll", "BINARY"),
            ("PySide6/plugins/platforms/qwindows.dll", "C:/qt/qwindows.dll", "BINARY"),
            ("PySide6/Qt6Pdf.dll", "C:/qt/Qt6Pdf.dll", "BINARY"),
        ]
        result = self.execute_spec(binaries)
        self.assertEqual(result["binaries"], binaries[2:])
        self.assertEqual(result["name"], "微信时间胶囊-v0.3.1-rc7-win64")

    def test_spec_refuses_stale_application_version(self):
        """Removing the source guard would package RC6 application code under an RC7 filename."""
        with self.assertRaisesRegex(ValueError, "application version mismatch"):
            self.execute_spec([], version="0.3.1-rc.6")

    def test_spec_refuses_mismatched_windows_product_version(self):
        """Removing the resource guard would label the RC7 EXE as RC6 in Windows."""
        with self.assertRaisesRegex(ValueError, "Windows resource mismatch"):
            self.execute_spec([], resource_version="0.3.1-rc.6")

    def test_qt_audit_fails_if_virtual_keyboard_is_shipped(self):
        """Removing the delivered-tree check would overlook VK reintroduced by a dependency hook."""
        good = {
            "distributions": {
                name: {"version": "6.11.2", "license_files": ["LICENSES/LGPL-3.0-only.txt"]}
                for name in ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6")
            },
            "dynamic_distribution": True,
            "upx": False,
            "shipped_qt_files": ["_internal/PySide6/Qt6Core.dll"],
        }
        for forbidden in (
            "_internal/PySide6/Qt6VirtualKeyboard.dll",
            "_internal/PySide6/plugins/platforminputcontexts/QTVIRTUALKEYBOARDPLUGIN.DLL",
        ):
            with self.subTest(forbidden=forbidden):
                issues = validate_qt_audit({**good, "shipped_qt_files": [forbidden]})
                self.assertTrue(any("VirtualKeyboard" in issue for issue in issues), issues)


if __name__ == "__main__":
    unittest.main()
