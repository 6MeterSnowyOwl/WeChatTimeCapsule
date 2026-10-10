from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QStandardPaths
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel
from PySide6.QtTest import QTest

from wechat_exporter import __version__
from wechat_exporter.account import AccountChoice, BackendInfo
import wechat_exporter.compatibility as compatibility_module
from wechat_exporter.compatibility import (
    CapabilityProbeResult,
    CompatibilityStatus,
    ProbeErrorCode,
    RuntimeCapability,
    VersionVerification,
    WeChatCompatibility,
    classify_wechat_version,
)
from wechat_exporter.diagnostics import generate_diagnostics
from wechat_exporter.archive import create_target
from wechat_exporter.gui.dialogs import (
    AccountSelectorDialog,
    DeleteSnapshotDialog,
    OnboardingDialog,
)
from wechat_exporter.gui.errors import map_exception
from wechat_exporter.gui.main_window import TargetCard
from wechat_exporter.integrity import generate_current_manifest
from wechat_exporter.registry import ArchiveRegistry
from wechat_exporter.models import ContactChoice
import wechat_exporter.service as service_module
from wechat_exporter.service import (
    AppSettings,
    ExporterService,
    SnapshotSummary,
    TargetSummary,
)
from wechat_exporter.utils import write_json_atomic


TESTED = WeChatCompatibility(
    True, "4.1.13.63", CompatibilityStatus.TESTED, "✓ 已验证"
)


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


class FakeDB:
    def get_self_info(self):
        return {"nick_name": "Synthetic Contact", "remark": ""}


def account(identity: str, index: int = 1) -> AccountChoice:
    return AccountChoice(
        identity,
        identity + "_locator",
        f"微信账号 {index}",
        "••••" + identity[-4:],
        float(index),
    )


def archive_fixture(root: Path, schema_version: int = 2) -> Path:
    target = root / "Synthetic Contact"
    snapshot = target / "snapshots" / "20260912_190000"
    snapshot.mkdir(parents=True)
    (snapshot / "chat.md").write_text(
        "# Synthetic Contact\nSYNTHETIC_CHAT_TEXT\n", encoding="utf-8"
    )
    write_json_atomic(
        snapshot / "export_meta.json",
        {
            "status": "COMPLETE",
            "snapshot_id": "20260912_190000",
            "message_count": 1,
            "new_message_count": 1,
            "created_at": "2026-09-12T19:00:00+09:00",
            "backup_mode": "quick",
        },
    )
    write_json_atomic(
        target / "target.json",
        {
            "schema_version": schema_version,
            "username": "internal_supersecret123",
            "display_name": "Synthetic Contact",
        },
    )
    write_json_atomic(
        target / "archive_state.json",
        {"schema_version": schema_version, "message_count": 1},
    )
    write_json_atomic(
        target / "snapshot_index.json",
        {
            "schema_version": schema_version,
            "snapshots": [
                {
                    "id": "20260912_190000",
                    "created_at": "2026-09-12T19:00:00+09:00",
                    "message_count": 1,
                    "new_message_count": 1,
                    "status": "COMPLETE",
                }
            ],
        },
    )
    (target / "state").mkdir()
    write_json_atomic(target / "state" / "schema.json", {"schema_version": 1})
    (target / "state" / "messages.jsonl").write_text(
        json.dumps({"content": "SYNTHETIC_CHAT_TEXT"}) + "\n",
        encoding="utf-8",
    )
    return target


def service_fixture(
    root: Path,
    accounts: list[AccountChoice],
    *,
    database_factory=None,
    probe_runner=None,
    compatibility_provider=None,
) -> ExporterService:
    project = root / "project"
    project.mkdir(exist_ok=True)
    settings = AppSettings(root / "appdata" / "settings.json", project / "exports")
    settings.data["archive_root"] = str(root / "archives")
    registry = ArchiveRegistry(root / "appdata" / "archive_registry.json")
    factory = database_factory or Mock(
        side_effect=lambda identity: (
            FakeDB(), BackendInfo("1.2.1", identity + "_locator", identity)
        )
    )
    with patch("wechat_exporter.service.configure_logging", return_value=Mock()):
        return ExporterService(
            project,
            settings=settings,
            registry=registry,
            account_lister=Mock(return_value=accounts),
            database_factory=factory,
            compatibility_provider=compatibility_provider or Mock(return_value=TESTED),
            probe_runner=probe_runner or Mock(
                return_value=CapabilityProbeResult(RuntimeCapability.READY)
            ),
        )


class PublicBetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_single_account_is_selected_without_dialog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [account("internal_one")])
            status = service.detect_wechat()
            self.assertFalse(status.selection_required)
            self.assertTrue(status.connected)
            self.assertEqual(service.settings.selected_account_identity, "internal_one")

    def test_probe_display_name_is_used_for_gui_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(
                root,
                [account("internal_one")],
                probe_runner=Mock(
                    return_value=CapabilityProbeResult(
                        RuntimeCapability.READY,
                        account_display_name="Alice",
                    )
                ),
            )

            status = service.detect_wechat()

            self.assertEqual(status.account_name, "Alice")
            self.assertNotEqual(status.account_name, "微信账号 1")

    def test_multiple_accounts_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = Mock()
            service = service_fixture(
                root, [account("internal_one", 1), account("internal_two", 2)],
                database_factory=factory,
            )
            status = service.detect_wechat()
            self.assertTrue(status.selection_required)
            self.assertEqual(status.account_count, 2)
            factory.assert_not_called()

    def test_remembered_account_missing_requires_new_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [account("internal_new")])
            service.settings.data["selected_account_identity"] = "internal_missing"
            status = service.detect_wechat()
            self.assertTrue(status.selection_required)
            self.assertTrue(status.remembered_missing)

    def test_account_selector_cancel_does_not_initialize_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = Mock()
            service = service_fixture(
                root, [account("internal_one", 1), account("internal_two", 2)],
                database_factory=factory,
            )
            status = service.detect_wechat()
            dialog = AccountSelectorDialog(status.accounts)
            dialog.reject()
            factory.assert_not_called()

    def test_selected_account_identity_reaches_database_factory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = Mock(
                return_value=(FakeDB(), BackendInfo("1.2.1", "two_locator", "internal_two"))
            )
            service = service_fixture(
                root, [account("internal_one", 1), account("internal_two", 2)],
                database_factory=factory,
            )
            service.select_account("internal_two")
            service._db()
            factory.assert_called_once_with("internal_two")

    def test_untested_ready_allows_online_operations(self):
        """Catches the RC1 bug where an exact-version label blocks a readable DB."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [account("internal_one")])
            service.compatibility_provider = Mock(
                return_value=WeChatCompatibility(
                    True,
                    "4.1.13.65",
                    CompatibilityStatus.UNTESTED,
                    "⚠ 尚未验证",
                )
            )

            try:
                service._ensure_backup_compatible()
            except RuntimeError as exc:
                self.fail(f"UNTESTED with a readable database was blocked: {exc}")

    def test_dual_compatibility_state_controls_online_operations(self):
        """Catches any return to using exact-version verification as runtime health."""
        verification = getattr(compatibility_module, "VersionVerification", None)
        capability = getattr(compatibility_module, "RuntimeCapability", None)
        allowed = getattr(compatibility_module, "operations_allowed", None)
        self.assertIsNotNone(verification, "VersionVerification is missing")
        self.assertIsNotNone(capability, "RuntimeCapability is missing")
        self.assertIsNotNone(allowed, "operations_allowed is missing")
        cases = (
            (verification.UNTESTED, capability.READY, True),
            (verification.UNTESTED, capability.PROBE_FAILED, False),
            (verification.TESTED, capability.PROBE_FAILED, False),
            (verification.UNSUPPORTED, capability.NOT_PROBED, False),
        )
        for version_state, runtime_state, expected in cases:
            with self.subTest(version_state=version_state, runtime_state=runtime_state):
                self.assertEqual(allowed(version_state, runtime_state), expected)

    def test_untested_ready_status_allows_but_probe_failure_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            untested = Mock(
                return_value=WeChatCompatibility(
                    True, "4.1.13.65", VersionVerification.UNTESTED, "⚠ 尚未验证"
                )
            )
            ready_service = service_fixture(
                root,
                [account("internal_one")],
                compatibility_provider=untested,
            )
            ready = ready_service.detect_wechat()
            self.assertEqual(ready.version_verification, VersionVerification.UNTESTED)
            self.assertEqual(ready.runtime_capability, RuntimeCapability.READY)
            self.assertTrue(ready.operations_allowed)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed_service = service_fixture(
                root,
                [account("internal_one")],
                compatibility_provider=untested,
                probe_runner=Mock(
                    return_value=CapabilityProbeResult(
                        RuntimeCapability.PROBE_FAILED, ProbeErrorCode.TIMEOUT
                    )
                ),
            )
            failed = failed_service.detect_wechat()
            self.assertFalse(failed.operations_allowed)
            self.assertEqual(failed.probe_error_code, ProbeErrorCode.TIMEOUT)
            with self.assertRaisesRegex(RuntimeError, "PROBE_FAILED"):
                failed_service._ensure_backup_compatible()

    def test_unsupported_version_is_not_probed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = Mock()
            status = service_fixture(
                root,
                [account("internal_one")],
                compatibility_provider=Mock(
                    return_value=WeChatCompatibility(
                        True, "3.9.12.1", VersionVerification.UNSUPPORTED, "✕ 当前不支持"
                    )
                ),
                probe_runner=probe,
            ).detect_wechat()
            self.assertEqual(status.runtime_capability, RuntimeCapability.NOT_PROBED)
            self.assertFalse(status.operations_allowed)
            probe.assert_not_called()

    def test_local_registry_targets_remain_visible_when_wechat_not_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = archive_fixture(root / "external")
            service = service_fixture(
                root,
                [],
                compatibility_provider=Mock(
                    return_value=WeChatCompatibility(
                        False, "", VersionVerification.UNTESTED, "○ 未检测到"
                    )
                ),
            )
            service.registry.register(target)
            self.assertEqual(
                service.detect_wechat().runtime_capability,
                RuntimeCapability.WECHAT_NOT_RUNNING,
            )
            self.assertEqual(len(service.list_targets()), 1)

    def test_untested_ready_does_not_filter_local_registry_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = archive_fixture(root / "external")
            service = service_fixture(
                root,
                [account("internal_one")],
                compatibility_provider=Mock(
                    return_value=WeChatCompatibility(
                        True, "4.1.13.65", VersionVerification.UNTESTED, "⚠ 尚未验证"
                    )
                ),
            )
            service.registry.register(target)
            self.assertTrue(service.detect_wechat().operations_allowed)
            self.assertEqual(len(service.list_targets()), 1)

    def test_fresh_archive_root_uses_qt_documents_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                QStandardPaths, "writableLocation", return_value=str(root / "Documents")
            ):
                settings = AppSettings(root / "appdata" / "settings.json", root / "project")
                self.assertEqual(
                    settings.archive_root, root / "Documents" / "微信时间胶囊"
                )
                self.assertTrue(settings.path.is_file())

    def test_fresh_registry_is_materialized_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "appdata" / "archive_registry.json"
            registry = ArchiveRegistry(path)
            self.assertTrue(path.is_file())
            self.assertEqual(registry.entries(), [])

    def test_new_appdata_uses_product_slug_but_legacy_config_stays_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}):
                fresh = AppSettings.default(root / "project")
                self.assertEqual(fresh.path.parent.name, "WeChatTimeCapsule")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}):
                legacy_path = root / "WeChatMarkdownExporter" / "settings.json"
                legacy = AppSettings(legacy_path, root / "project" / "exports")
                existing = AppSettings.default(root / "project")
                self.assertEqual(existing.path, legacy_path)

    def test_registry_records_exact_archive_path_without_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = archive_fixture(root / "external")
            before = (target / "chat.md").read_bytes() if (target / "chat.md").exists() else None
            registry = ArchiveRegistry(root / "appdata" / "registry.json")
            entry = registry.register(target)
            self.assertEqual(entry.path, target.resolve())
            self.assertEqual(len(registry.entries()), 1)
            self.assertEqual((target / "chat.md").read_bytes() if (target / "chat.md").exists() else None, before)

    def test_remove_backup_target_removes_only_exact_archive_id_and_preserves_trees(self):
        """Catches path deletion, display-name deletion, and removal of unrelated entries."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            first = archive_fixture(root / "first")
            second = archive_fixture(root / "second")
            second_target = json.loads((second / "target.json").read_text(encoding="utf-8"))
            second_target["username"] = "internal_other_ting"
            write_json_atomic(second / "target.json", second_target)
            first_entry = service.registry.register(first)
            second_entry = service.registry.register(second)
            before = {first: tree_hash(first), second: tree_hash(second)}

            result = service.remove_backup_target(first_entry.archive_id)

            self.assertTrue(result.removed)
            self.assertEqual(
                [entry.archive_id for entry in service.registry.entries()],
                [second_entry.archive_id],
            )
            self.assertEqual(tree_hash(first), before[first])
            self.assertEqual(tree_hash(second), before[second])
            self.assertEqual(
                [item.archive_id for item in service.list_targets()],
                [second_entry.archive_id],
            )

    def test_same_display_name_different_archive_ids_remove_independently(self):
        """Catches removing targets by display name instead of canonical registry ID."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            first = create_target(
                root / "first", ContactChoice.from_fields(username="stable-a", remark="Synthetic Contact")
            )
            second = create_target(
                root / "second", ContactChoice.from_fields(username="stable-b", remark="Synthetic Contact")
            )
            first_entry = service.registry.register(first.root)
            second_entry = service.registry.register(second.root)

            service.remove_backup_target(first_entry.archive_id)

            self.assertEqual(
                [entry.archive_id for entry in service.registry.entries()],
                [second_entry.archive_id],
            )
            self.assertEqual([item.display_name for item in service.list_targets()], ["Synthetic Contact"])

    def test_remove_backup_target_is_offline_and_missing_id_is_idempotent(self):
        """Catches removal being gated on WeChat/probe state or crashing when repeated."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_factory = Mock(side_effect=AssertionError("database must stay offline"))
            service = service_fixture(root, [], database_factory=database_factory)
            target = archive_fixture(root / "external")
            entry = service.registry.register(target)

            first = service.remove_backup_target(entry.archive_id)
            second = service.remove_backup_target(entry.archive_id)

            self.assertTrue(first.removed)
            self.assertFalse(second.removed)
            self.assertEqual(second.message, "该备份对象已经不在列表中。")
            self.assertTrue(target.is_dir())
            database_factory.assert_not_called()

    def test_registry_remove_write_failure_keeps_in_memory_entry(self):
        """Catches exposing a half-applied removal when atomic persistence fails."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = ArchiveRegistry(root / "registry.json")
            entry = registry.register(root / "archive")
            with patch(
                "wechat_exporter.registry.write_json_atomic",
                side_effect=OSError("synthetic write failure"),
            ):
                with self.assertRaises(OSError):
                    registry.remove(entry.archive_id)
            self.assertEqual(registry.entries(), [entry])

    def test_remove_backup_target_allows_missing_or_offline_root(self):
        """Catches requiring an archive drive or directory to be online for list removal."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            missing = root / "offline-drive" / "Synthetic Contact"
            entry = service.registry.register(missing)
            self.assertFalse(missing.exists())

            result = service.remove_backup_target(entry.archive_id)

            self.assertTrue(result.removed)
            self.assertEqual(service.registry.entries(), [])
            self.assertFalse(missing.exists())

    def test_remove_then_import_existing_reuses_same_archive_without_file_mutation(self):
        """Catches re-import copying data, generating an archive, or leaving duplicates."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            target = archive_fixture(root / "external")
            (target / "chat.md").write_text("# Synthetic Contact\n", encoding="utf-8")
            avatar = target / "assets" / "avatars" / "contact.png"
            avatar.parent.mkdir(parents=True)
            avatar.write_bytes(b"synthetic-avatar")
            write_json_atomic(
                target / "capsule_meta.json",
                {
                    "status": "COMPLETE",
                    "integrity_status": "complete",
                    "capsule_schema_version": 1,
                    "message_count": 1,
                    "last_saved_at": "2026-09-12T19:00:00+09:00",
                    "avatars": {"contact": {"path": "assets/avatars/contact.png"}},
                },
            )
            generate_current_manifest(target)
            original = service.registry.register(target)
            before = tree_hash(target)

            service.remove_backup_target(original.archive_id)
            summary = service.import_existing_archive(target)

            self.assertEqual(tree_hash(target), before)
            self.assertEqual(summary.root, target.resolve())
            self.assertGreater(summary.message_count, 0)
            self.assertTrue(summary.capsule_exists)
            self.assertTrue(summary.integrity_ok)
            self.assertEqual(summary.avatar_path, avatar)
            self.assertEqual(len(service.registry.entries()), 1)
            self.assertEqual(len(service.list_targets()), 1)

    def test_concurrent_add_of_same_identity_is_serialized_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            contact = ContactChoice.from_fields(
                username="stable-contact-1", remark="Synthetic Contact"
            )
            real_create = create_target
            gate = threading.Event()
            guard = threading.Lock()
            activity = {"current": 0, "maximum": 0, "calls": 0}
            results = []
            errors = []

            def controlled_create(exports_root, selected_contact):
                with guard:
                    activity["current"] += 1
                    activity["calls"] += 1
                    activity["maximum"] = max(
                        activity["maximum"], activity["current"]
                    )
                    if activity["calls"] == 2:
                        gate.set()
                gate.wait(0.25)
                try:
                    return real_create(exports_root, selected_contact)
                finally:
                    with guard:
                        activity["current"] -= 1

            def add():
                try:
                    results.append(service.add_target(contact))
                except BaseException as error:
                    errors.append(error)

            with patch(
                "wechat_exporter.service.create_target",
                side_effect=controlled_create,
            ):
                threads = [threading.Thread(target=add) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(activity["maximum"], 1)
            self.assertEqual(len({item.root.resolve() for item in results}), 1)
            self.assertEqual(len(service.list_targets()), 1)

    def test_target_listing_deduplicates_identity_not_display_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            same_identity_a = create_target(
                root / "external-a",
                ContactChoice.from_fields(username="stable-1", remark="Synthetic Contact"),
            )
            same_identity_b = create_target(
                root / "external-b",
                ContactChoice.from_fields(username="stable-1", remark="Synthetic Contact"),
            )
            same_display_other_identity = create_target(
                root / "external-c",
                ContactChoice.from_fields(username="stable-2", remark="Synthetic Contact"),
            )
            service = service_fixture(root, [])
            for target in (
                same_identity_a,
                same_identity_b,
                same_display_other_identity,
            ):
                service.registry.register(target.root)

            targets = service._targets()

            self.assertEqual(
                {target.username for target in targets}, {"stable-1", "stable-2"}
            )
            self.assertEqual([target.display_name for target in targets], ["Synthetic Contact", "Synthetic Contact"])

    def test_identity_dedup_prefers_existing_current_capsule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blank = create_target(
                root / "blank",
                ContactChoice.from_fields(username="stable-1", remark="Synthetic Contact"),
            )
            complete = archive_fixture(root / "complete")
            target_payload = json.loads(
                (complete / "target.json").read_text(encoding="utf-8")
            )
            target_payload["username"] = "stable-1"
            write_json_atomic(complete / "target.json", target_payload)
            (complete / "chat.md").write_text("# Synthetic Contact\n", encoding="utf-8")
            write_json_atomic(
                complete / "capsule_meta.json",
                {"capsule_schema_version": 1, "message_count": 1},
            )
            service = service_fixture(root, [])
            service.registry.register(blank.root)
            service.registry.register(complete)

            targets = service._targets()

            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].root, complete.resolve())

    def test_add_existing_identity_returns_preferred_registered_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            blank = create_target(
                service.settings.archive_root,
                ContactChoice.from_fields(username="stable-1", remark="Synthetic Contact"),
            )
            complete = archive_fixture(root / "complete")
            target_payload = json.loads(
                (complete / "target.json").read_text(encoding="utf-8")
            )
            target_payload["username"] = "stable-1"
            write_json_atomic(complete / "target.json", target_payload)
            (complete / "chat.md").write_text("# Synthetic Contact\n", encoding="utf-8")
            write_json_atomic(
                complete / "capsule_meta.json",
                {"capsule_schema_version": 1, "message_count": 1},
            )
            service.registry.register(blank.root)
            service.registry.register(complete)

            summary = service.add_target(
                ContactChoice.from_fields(username="stable-1", remark="Synthetic Contact")
            )

            self.assertEqual(summary.root, complete.resolve())
            self.assertEqual(
                [item.name for item in service.settings.archive_root.iterdir()],
                ["Synthetic Contact"],
            )

    def test_bootstrap_does_not_register_second_path_for_known_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            settings = AppSettings(
                root / "appdata" / "settings.json", project / "exports"
            )
            settings.data["archive_root"] = str(root / "active")
            blank = create_target(
                settings.archive_root,
                ContactChoice.from_fields(username="stable-1", remark="Synthetic Contact"),
            )
            complete = archive_fixture(root / "complete")
            target_payload = json.loads(
                (complete / "target.json").read_text(encoding="utf-8")
            )
            target_payload["username"] = blank.username
            write_json_atomic(complete / "target.json", target_payload)
            registry = ArchiveRegistry(
                root / "appdata" / "archive_registry.json"
            )
            registry.register(complete)

            with patch("wechat_exporter.service.configure_logging", return_value=Mock()):
                ExporterService(
                    project,
                    settings=settings,
                    registry=registry,
                    account_lister=Mock(return_value=[]),
                )

            self.assertEqual(len(registry.entries()), 1)
            self.assertEqual(registry.entries()[0].path, complete.resolve())

    def test_packaged_like_quick_save_uses_appdata_work_root(self):
        from tests.full_history_fixtures import make_fixture, make_service, close_test_logs
        from tests.one_shot_provider_fixtures import PlaintextDescriptor
        from wechat_exporter import one_shot_provider
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = make_fixture(root)
            service = make_service(root, fixture)
            service.project_root = root / 'program-files/application'
            service.project_root.mkdir(parents=True)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    outcome = service.quick_backup(fixture.archive)
                work_root = service.settings.app_data_dir / 'work'
                self.assertEqual(outcome.capsule.path, fixture.archive)
                self.assertEqual(outcome.capsule.message_count, 6)
                self.assertTrue(work_root.is_dir())
                self.assertEqual(list(work_root.iterdir()), [])
                self.assertFalse((service.project_root / 'work').exists())
                self.assertEqual(list(service.project_root.iterdir()), [])
            finally:
                close_test_logs(root)

    def test_runtime_work_directory_failure_has_stable_sanitized_code(self):
        error_type = getattr(service_module, "RuntimeWorkDirectoryError", None)
        self.assertIsNotNone(error_type)
        mapped = map_exception(error_type())
        self.assertEqual(mapped.code, "LOCAL_WORKSPACE_UNAVAILABLE")
        self.assertNotIn("Users", mapped.message)
        self.assertNotIn("Users", mapped.technical_summary)

    def test_registry_listing_uses_only_the_exact_registered_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registered = archive_fixture(root / "external")
            service = service_fixture(root, [])
            service.registry.register(registered)
            with patch("wechat_exporter.service.discover_targets", return_value=[]) as discover:
                listed = service.list_targets()
            self.assertEqual([item.root for item in listed], [registered.resolve()])
            discover.assert_not_called()

    def test_removed_managed_root_stays_unlisted_after_service_restart(self):
        """Catches bootstrap silently re-adding an explicitly removed archive."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = service_fixture(root, [])
            target = archive_fixture(service.settings.archive_root)
            entry = service.registry.register(target)
            service.remove_backup_target(entry.archive_id)

            restarted = service_fixture(root, [])

            self.assertEqual(restarted.registry.entries(), [])
            self.assertEqual(restarted.list_targets(), [])
            self.assertTrue(target.is_dir())

    def test_import_existing_archive_registers_without_copying(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = archive_fixture(root / "external")
            chat = target / "snapshots" / "20260912_190000" / "chat.md"
            before = chat.read_bytes()
            service = service_fixture(root, [])
            summary = service.import_existing_archive(target)
            self.assertEqual(summary.root, target.resolve())
            self.assertEqual(chat.read_bytes(), before)
            self.assertEqual(service.registry.entries()[0].path, target.resolve())

    def test_future_archive_schema_fails_closed_during_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = archive_fixture(root / "external", schema_version=999)
            service = service_fixture(root, [])
            with self.assertRaisesRegex(RuntimeError, "更新版本"):
                service.import_existing_archive(target)
            self.assertEqual(service.registry.entries(), [])

    def test_settings_write_is_atomic_and_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = AppSettings(root / "appdata" / "settings.json", root / "project")
            settings.set_archive_root(root / "new")
            loaded = json.loads(settings.path.read_text(encoding="utf-8"))
            self.assertEqual(Path(loaded["archive_root"]), (root / "new").resolve())
            self.assertFalse(settings.path.with_suffix(".json.part").exists())

    def test_first_backup_notice_suppression_uses_archive_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = AppSettings(root / "appdata" / "settings.json", root / "project")
            settings.suppress_first_backup_notice("archive-id-1")
            reloaded = AppSettings(settings.path, root / "project")
            self.assertTrue(reloaded.first_backup_notice_suppressed("archive-id-1"))
            self.assertFalse(reloaded.first_backup_notice_suppressed("archive-id-2"))

    def test_corrupt_config_recovers_without_touching_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = archive_fixture(root / "archives")
            chat = target / "snapshots" / "20260912_190000" / "chat.md"
            before = chat.read_bytes()
            path = root / "appdata" / "settings.json"
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")
            settings = AppSettings(path, root / "project")
            self.assertIn("设置损坏", settings.recovery_notice)
            self.assertTrue(list(path.parent.glob("settings.json.corrupt.*")))
            self.assertEqual(chat.read_bytes(), before)

    def test_diagnostics_privacy_regression_has_zero_sensitive_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal_id_fixture = "wx" + "id_fixture_secret"
            target = archive_fixture(root / "archives")
            service = service_fixture(
                root,
                [account("internal_supersecret123")],
                probe_runner=Mock(
                    return_value=CapabilityProbeResult(
                        RuntimeCapability.READY,
                        account_display_name="Alice",
                    )
                ),
            )
            service.registry.register(target)
            log = service.settings.app_data_dir / "logs" / "gui.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                f"Synthetic Contact internal_supersecret123249 {internal_id_fixture} SYNTHETIC_CHAT_TEXT "
                "FAKE_DATABASE_KEY C:\\Users\\TestUser\\ private_token=abc123\n",
                encoding="utf-8",
            )
            result = generate_diagnostics(service)
            with zipfile.ZipFile(result.path) as archive:
                combined = "\n".join(
                    archive.read(name).decode("utf-8") for name in archive.namelist()
                )
                app_info = json.loads(archive.read("diagnostics/app.json"))
                compatibility_info = json.loads(
                    archive.read("diagnostics/compatibility.json")
                )
            for forbidden in (
                "Synthetic Contact",
                "internal_supersecret123",
                internal_id_fixture,
                "SYNTHETIC_CHAT_TEXT",
                "FAKE_DATABASE_KEY",
                "C:\\Users\\TestUser\\",
                "abc123",
                "Alice",
            ):
                self.assertNotIn(forbidden, combined)
            self.assertEqual(result.sensitive_hits, 0)
            self.assertEqual(app_info["product_name"], "微信时间胶囊")
            self.assertEqual(app_info["product_version"], __version__)
            self.assertEqual(app_info["mutable_schema_version"], 2)
            self.assertEqual(app_info["snapshot_schema_version"], 1)
            self.assertEqual(app_info["message_schema_version"], 2)
            self.assertEqual(compatibility_info["verification"], "tested")
            self.assertEqual(compatibility_info["runtime_capability"], "ready")
            self.assertEqual(compatibility_info["probe_error_code"], "none")
            self.assertNotIn("account_name", compatibility_info)
            self.assertEqual(
                sorted(Path(name).name for name in zipfile.ZipFile(result.path).namelist()),
                [
                    "app.json",
                    "compatibility.json",
                    "environment.json",
                    "integrity_summary.json",
                    "recent_logs.txt",
                ],
            )

    def test_diagnostics_preserve_tested_and_untested_after_gui_hides_them(self):
        """Catches deleting internal verification data merely to simplify the GUI."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (VersionVerification.TESTED, "4.1.13.63", "tested"),
                (VersionVerification.TESTED, "4.1.13.65", "tested"),
                (VersionVerification.UNTESTED, "4.1.99.1", "untested"),
            )
            for index, (verification, version, expected) in enumerate(cases):
                with self.subTest(verification=verification):
                    case_root = root / str(index)
                    case_root.mkdir()
                    service = service_fixture(
                        case_root,
                        [account(f"internal_case_{index}")],
                        compatibility_provider=Mock(
                            return_value=WeChatCompatibility(
                                True, version, verification, "internal-only"
                            )
                        ),
                    )
                    result = generate_diagnostics(service)
                    with zipfile.ZipFile(result.path) as archive:
                        compatibility_info = json.loads(
                            archive.read("diagnostics/compatibility.json")
                        )
                    self.assertEqual(compatibility_info["verification"], expected)
                    self.assertEqual(compatibility_info["runtime_capability"], "ready")

    def test_compatibility_classifies_tested_untested_and_unsupported(self):
        self.assertEqual(
            classify_wechat_version("4.1.13.63"), CompatibilityStatus.TESTED
        )
        self.assertEqual(
            classify_wechat_version("4.1.13.65"), CompatibilityStatus.TESTED
        )
        self.assertEqual(
            classify_wechat_version("4.1.99.1"), CompatibilityStatus.UNTESTED
        )
        self.assertEqual(
            classify_wechat_version("5.0.0.0"), CompatibilityStatus.UNSUPPORTED
        )

    def test_unknown_running_version_is_not_eligible_for_probe(self):
        with (
            patch.object(compatibility_module, "_running_wechat_paths", return_value=["weixin.exe"]),
            patch.object(compatibility_module, "_file_version", return_value=""),
        ):
            result = compatibility_module.detect_wechat_compatibility()
        self.assertTrue(result.detected)
        self.assertEqual(result.status, VersionVerification.UNSUPPORTED)

    def test_public_beta_failures_have_actionable_gui_messages(self):
        cases = (
            (
                RuntimeError("检测到多个或已变化的微信账号，请先明确选择。"),
                "WECHAT_ACCOUNT_SELECTION_REQUIRED",
            ),
            (
                RuntimeError("当前微信版本 5.0.0.0 不受支持；本次操作没有继续。"),
                "WECHAT_VERSION_UNSUPPORTED",
            ),
            (
                ValueError("所选目录不是完整的微信时间胶囊备份对象。"),
                "INVALID_ARCHIVE_IMPORT",
            ),
            (
                RuntimeError("诊断报告隐私自检失败，未生成文件。"),
                "DIAGNOSTICS_PRIVACY_CHECK_FAILED",
            ),
        )
        for error, expected_code in cases:
            with self.subTest(expected_code):
                self.assertEqual(map_exception(error).code, expected_code)

    def test_corrupt_registry_recovers_without_scanning(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry.json"
            path.write_text("not-json", encoding="utf-8")
            registry = ArchiveRegistry(path)
            self.assertIn("登记表损坏", registry.recovery_notice)
            self.assertEqual(registry.entries(), [])
            self.assertTrue(list(path.parent.glob("registry.json.corrupt.*")))

    def test_account_selector_never_displays_raw_identity(self):
        choice = account("internal_supersecret123")
        dialog = AccountSelectorDialog([choice])
        visible = " ".join(radio.text() for radio in dialog.radios)
        self.assertNotIn(choice.stable_id, visible)
        self.assertIn(choice.masked_identity, visible)
        dialog.close()

    def test_dialog_tab_navigation_and_default_enter(self):
        choices = [account("internal_one", 1), account("internal_two", 2)]
        dialog = AccountSelectorDialog(choices)
        dialog.show()
        dialog.activateWindow()
        self.app.processEvents()
        dialog.radios[0].setFocus(Qt.TabFocusReason)
        self.app.processEvents()
        visited = {id(self.app.focusWidget())}
        for _ in range(5):
            QTest.keyClick(dialog, Qt.Key_Tab)
            self.app.processEvents()
            visited.add(id(self.app.focusWidget()))
        self.assertGreater(len(visited), 1)
        confirm = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        confirm.setFocus()
        QTest.keyClick(confirm, Qt.Key_Return)
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_target_card_keyboard_enter_activates(self):
        summary = TargetSummary(Path("target"), "Example", 0, None, "○ 尚未保存", None, None, False, 0, 0)
        card = TargetCard(summary)
        activated = []
        card.expanded_changed.connect(lambda item, expanded: activated.append(item) if expanded else None)
        card.show()
        card.setFocus()
        QTest.keyClick(card, Qt.Key_Enter)
        self.assertEqual(activated, [summary])
        card.close()

    def test_onboarding_has_three_pages_and_escape_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = AppSettings(root / "settings.json", root / "project")
            dialog = OnboardingDialog(settings)
            self.assertEqual(dialog.pages.count(), 3)
            dialog.show()
            QTest.keyClick(dialog, Qt.Key_Escape)
            self.assertFalse(dialog.isVisible())

    def test_delete_confirmation_is_explicit_and_danger_styled(self):
        snapshot = SnapshotSummary(
            "id", Path("target"), "2026-09-12T19:00:00+09:00",
            4, 0, "quick", "✓ 完整", True,
        )
        dialog = DeleteSnapshotDialog(snapshot)
        visible = " ".join(label.text() for label in dialog.findChildren(QLabel))
        delete = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        cancel = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel)
        self.assertEqual(dialog.windowTitle(), "删除这份时间胶囊？")
        self.assertIn("不会删除微信里的聊天", visible)
        self.assertIn("无法从微信时间胶囊中恢复", visible)
        self.assertEqual(delete.text(), "删除")
        self.assertEqual(delete.objectName(), "dangerButton")
        self.assertFalse(delete.isDefault())
        self.assertTrue(cancel.isDefault())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
