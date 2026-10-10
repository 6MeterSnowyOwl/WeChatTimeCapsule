"""Product lifecycle acceptance: every input lives in an explicit temp tree."""
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime
import json
import logging
from contextlib import closing
import sqlite3
from unittest.mock import patch
import unittest

from tests.one_shot_fixtures import make_fixture, file_hashes
from wechat_exporter.one_shot_export import export_full_history_once


class ProductHistoryTests(unittest.TestCase):
    def test_original_service_v2_full_restart_quick_and_repeated_full(self):
        from tests.one_shot_provider_fixtures import PlaintextDescriptor
        from tests.full_history_fixtures import make_fixture
        from wechat_exporter import one_shot_provider
        from wechat_exporter.service import ExporterService
        from wechat_exporter.history_contracts import ArchiveScope
        from wechat_exporter.product_history import read_product
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root)
            before = file_hashes(f.source)
            old_v2 = file_hashes(f.archive)
            service = make_product_service(root, f, ExporterService)
            try:
                target = service.import_existing_archive(f.archive)
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    outcome = service.reconciliation_backup(target.root)
                    output = outcome.capsule.path
                    self.assertEqual(output, f.archive)
                    backup = Path(json.loads((output / 'capsule_meta.json').read_text(encoding='utf8'))['v2_backup'])
                    self.assertEqual(file_hashes(backup), old_v2)
                    self.assertEqual(outcome.capsule.message_count, 6)
                    bundle, cursors = read_product(output, ArchiveScope('synthetic-stable', 'synthetic-conversation'))
                    self.assertEqual(len(cursors), 2)
                    self.assertEqual(bundle.data_generation, 1)
                    original_id = bundle.archive_id
                    self.assertIn('legacy body retained', (output / 'chat.md').read_text(encoding='utf8'))
                    self.assertEqual(before, file_hashes(f.source))
                    service = make_product_service(root, f, ExporterService)
                    with closing(sqlite3.connect(f.source / 'message/message_1.db')) as connection:
                        connection.execute('INSERT INTO "' + f.table + '" VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (21, 1, 106, 2, 1700000100, 'after restart', b'', b'', None, 800))
                        connection.commit()
                    before = file_hashes(f.source)
                    outcome = service.quick_backup(output)
                    self.assertEqual(outcome.capsule.message_count, 7)
                    self.assertEqual(outcome.capsule.new_message_count, 1)
                    self.assertEqual(outcome.capsule.save_mode, 'quick')
                    outcome = service.reconciliation_backup(output)
                    self.assertEqual(outcome.capsule.new_message_count, 0)
                    final, _ = read_product(output, ArchiveScope('synthetic-stable', 'synthetic-conversation'))
                    self.assertEqual(final.archive_id, original_id)
                    self.assertEqual(final.data_generation, 3)
                    self.assertEqual(before, file_hashes(f.source))
                    self.assertEqual(file_hashes(backup), old_v2)
                    self.assertEqual(service.list_targets()[0].root, output)
                    self.assertTrue(service.list_targets()[0].integrity_ok)
            finally:
                close_test_logs(root)

    def test_fresh_full_accepts_no_v2_input_and_reads_all_shards(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root / 'input')
            before = file_hashes(f.root)
            try:
                result = export_full_history_once(mode='synthetic', account='synthetic-account',
                    conversation='synthetic-conversation', archive=None,
                    source_manifest=f.manifest, media_root=f.media, output=root / 'output')
            except (TypeError, ValueError, FileNotFoundError) as exc:
                self.fail('Fresh full save must accept no archive input: ' + type(exc).__name__)
            self.assertEqual(result['message_count'], 5)
            self.assertEqual(result['counts']['raw_source_count'], 6)
            self.assertEqual(result['counts']['archive_before_count'], 0)
            for path in (root / 'output/assets').rglob('*'):
                if path.is_file():
                    durable = root / 'output/state' / path.relative_to(root / 'output')
                    self.assertTrue(durable.is_file(), 'Reusable product media requires a durable asset')
                    self.assertEqual(path.read_bytes(), durable.read_bytes())
            self.assertEqual(before, file_hashes(f.root))

    def test_validation_failure_rolls_back_complete_staged_commit(self):
        from wechat_exporter.current_capsule import _commit_current_capsule, recover_current_capsule_transaction
        from wechat_exporter.integrity import generate_current_manifest
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target, staging = root / 'target', root / 'staging'
            for path, content in ((target, 'previous'), (staging, 'replacement')):
                path.mkdir()
                (path / 'chat.md').write_text(content, encoding='utf8')
                (path / 'capsule_meta.json').write_text('{}', encoding='utf8')
                generate_current_manifest(path)
            before = file_hashes(target)
            def reject(_root):
                raise ValueError('deliberate failed readback')
            try:
                with self.assertRaisesRegex(ValueError, 'deliberate failed readback'):
                    _commit_current_capsule(target, staging, datetime.now().astimezone(), validate=reject)
            except TypeError as exc:
                self.fail('Transaction must validate before committed marker: ' + str(exc))
            recover_current_capsule_transaction(target)
            self.assertEqual(before, file_hashes(target))

    def test_failed_commit_marker_and_failed_rollback_keep_recovery_evidence(self):
        from wechat_exporter import current_capsule as current
        from wechat_exporter.integrity import generate_current_manifest
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target, staging = root / 'target', root / 'staging'
            for path, content in ((target, 'previous'), (staging, 'replacement')):
                path.mkdir()
                (path / 'chat.md').write_text(content, encoding='utf8')
                (path / 'capsule_meta.json').write_text('{}', encoding='utf8')
                generate_current_manifest(path)
            before = file_hashes(target)
            write = current.write_json_atomic
            def fail_marker(path, value):
                if value.get('phase') == 'committed':
                    raise OSError('synthetic commit marker fault')
                return write(path, value)
            with patch.object(current, 'write_json_atomic', side_effect=fail_marker), patch.object(
                    current, '_restore_current_transaction', side_effect=OSError('synthetic rollback interruption')):
                with self.assertRaisesRegex(OSError, 'synthetic rollback interruption'):
                    current._commit_current_capsule(target, staging, datetime.now().astimezone())
            self.assertTrue((target / current.CURRENT_JOURNAL).is_file())
            self.assertTrue((target / current.CURRENT_TRANSACTION).is_dir())
            current.recover_current_capsule_transaction(target)
            self.assertEqual(before, file_hashes(target))


def close_test_logs(root):
    for logger in (logging.getLogger(), *[x for x in logging.Logger.manager.loggerDict.values()
                                        if isinstance(x, logging.Logger)]):
        for handler in list(logger.handlers):
            if getattr(handler, 'baseFilename', '').startswith(str(root)):
                logger.removeHandler(handler)
                handler.close()


def make_product_service(root, fixture, service_type=None):
    from wechat_exporter.account import AccountChoice, BackendInfo
    from wechat_exporter.compatibility import WeChatCompatibility, CompatibilityStatus, CapabilityProbeResult, RuntimeCapability
    from wechat_exporter.service import AppSettings
    if service_type is None:
        from wechat_exporter.service import ExporterService
        service_type = ExporterService
    path = root / 'application/settings.json'
    if not path.exists():
        path.parent.mkdir()
        path.write_text(json.dumps(dict(archive_root=str(root / 'old-exports'),
            known_archive_roots=[], selected_account_identity='synthetic-stable',
            onboarding_completed=True)), encoding='utf8')
    settings = AppSettings(path, root / 'unused-exports')
    selected = AccountChoice('synthetic-stable', 'synthetic-account', 'Synthetic Account', 'test', 1)
    return service_type(root, settings=settings, account_lister=lambda: [selected],
        database_factory=lambda identity: (fixture.runtime.base_db, BackendInfo('1.2.1', 'synthetic-account', 'synthetic-stable')),
        compatibility_provider=lambda: WeChatCompatibility(True, '4.1.13.63', CompatibilityStatus.TESTED, 'Synthetic'),
        probe_runner=lambda identity: CapabilityProbeResult(RuntimeCapability.READY))


if __name__ == '__main__':
    unittest.main()
