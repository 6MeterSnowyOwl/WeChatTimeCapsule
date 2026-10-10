import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.full_history_fixtures import make_fixture, make_service, close_test_logs
from tests.one_shot_fixtures import file_hashes
from tests.one_shot_provider_fixtures import PlaintextDescriptor
from wechat_exporter import one_shot_provider


class ExistingFullHistoryTests(unittest.TestCase):
    def test_wrong_account_scope_is_rejected_before_runtime_or_contact_read(self):
        from wechat_exporter.account import AccountChoice
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root)
            service = make_service(root, f)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    service.reconciliation_backup(f.archive)
                service._database = None
                service._selected_account_identity = 'other-stable'
                service.account_lister = lambda: [AccountChoice('other-stable', 'other-locator', 'Other Synthetic', 'test', 1)]
                def forbidden_runtime(_selected):
                    self.fail('Scope mismatch must reject before obtaining another runtime')
                service.database_factory = forbidden_runtime
                before = file_hashes(f.archive)
                with self.assertRaises(ValueError):
                    service.quick_backup(f.archive)
                self.assertEqual(file_hashes(f.archive), before)
            finally:
                close_test_logs(root)
    def test_fresh_original_target_and_corrupt_missing_store_are_distinct(self):
        from wechat_exporter.models import ContactChoice
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root)
            service = make_service(root, f)
            try:
                service.settings.data['archive_root'] = str(root / 'fresh-exports')
                # Another conversation is absent in every shard: valid zero-message output.
                fresh = service.add_target(ContactChoice.from_fields(username='synthetic-empty', nickname='Synthetic Empty'))
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    result = service.quick_backup(fresh.root)
                self.assertEqual(result.capsule.message_count, 0)
                self.assertEqual(result.capsule.save_mode, 'reconciliation')
                self.assertTrue(service.verify_capsule(fresh.root).ok)
                (f.archive / 'state/messages.jsonl').unlink()
                corrupted = file_hashes(f.archive)
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    with self.assertRaisesRegex(ValueError, 'CORRUPT_MISSING_MESSAGE_STATE'):
                        service.reconciliation_backup(f.archive)
                self.assertEqual(file_hashes(f.archive), corrupted)
            finally:
                close_test_logs(root)

    def test_bad_cursor_scope_and_media_reject_without_target_writes(self):
        from wechat_exporter.integrity import generate_current_manifest, sha256_file
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root)
            service = make_service(root, f)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    service.reconciliation_backup(f.archive)
                    for leaf, mutation in (
                        ('state/source_cursors.json', lambda value: {**value, 'data_generation': 99}),
                        ('state/schema.json', lambda value: {**value, 'scope': {'account_id': 'wrong-account', 'conversation_id': 'synthetic-conversation'}}),
                    ):
                        path = f.archive / leaf
                        saved = path.read_bytes()
                        path.write_text(json.dumps(mutation(json.loads(saved))), encoding='utf8')
                        before = file_hashes(f.archive)
                        with self.assertRaises(ValueError):
                            service.quick_backup(f.archive)
                        self.assertEqual(file_hashes(f.archive), before)
                        path.write_bytes(saved)
                    path = f.archive / 'state/assets/images/old.png'
                    original = path.read_bytes()
                    path.write_bytes(b'bad synthetic media')
                    before = file_hashes(f.archive)
                    with self.assertRaises(ValueError):
                        service.quick_backup(f.archive)
                    self.assertEqual(file_hashes(f.archive), before)
                    path.write_bytes(original)
            finally:
                close_test_logs(root)

    def test_failed_readback_rolls_back_same_root_and_restart_recovers(self):
        from wechat_exporter import product_history
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root)
            service = make_service(root, f)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    service.reconciliation_backup(f.archive)
                    previous = file_hashes(f.archive)
                    real_read = product_history.read_product
                    def fail_new_generation(path, scope):
                        bundle, cursors = real_read(path, scope)
                        if path == f.archive and bundle.data_generation == 2:
                            raise ValueError('synthetic committed-file readback fault')
                        return bundle, cursors
                    with patch.object(product_history, 'read_product', side_effect=fail_new_generation):
                        with self.assertRaisesRegex(ValueError, 'synthetic committed-file readback fault'):
                            service.quick_backup(f.archive)
                service = make_service(root, f)
                self.assertEqual(file_hashes(f.archive), previous)
                self.assertTrue(service.verify_capsule(f.archive).ok)
            finally:
                close_test_logs(root)

    def test_original_gui_buttons_reopen_same_object_and_generate_files(self):
        from tests.product_gui_fixture import exercise
        with TemporaryDirectory() as directory:
            result = exercise(Path(directory), avatar_changes=True)
            self.assertTrue(result['same_target_root'])
            self.assertEqual([step['count'] for step in result['steps']], [6, 8, 8, 8])
            self.assertEqual([step['newly_discovered'] for step in result['steps']], [3, 2, 0, 0])
            self.assertTrue(result['retained_asset_identities_unchanged'])
            self.assertEqual([step['avatar_history_count'] for step in result['steps']], [2, 3, 3, 3])
            self.assertEqual([step['report_count'] for step in result['steps']], [1, 1, 2, 3])

    def test_original_full_reads_two_shards_into_same_root_after_complete_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            f = make_fixture(root)
            service = make_service(root, f)
            original = file_hashes(f.archive)
            source = file_hashes(f.source)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    outcome = service.reconciliation_backup(f.archive)
                self.assertEqual(outcome.capsule.message_count, 6,
                    'Original full button must retain both shards plus archive-only history')
                self.assertEqual(outcome.capsule.path, f.archive)
                self.assertEqual(file_hashes(f.source), source)
                schema = json.loads((f.archive / 'state/schema.json').read_text(encoding='utf8'))
                self.assertEqual(schema['schema_version'], 3)
                from wechat_exporter.product_history import read_product
                from wechat_exporter.history_contracts import ArchiveScope
                bundle, cursors = read_product(f.archive, ArchiveScope('synthetic-stable', 'synthetic-conversation'))
                records = bundle.to_core_records()
                self.assertEqual([row.identity for row in records[:5]],
                    ['server:101', 'server:102', 'server:105', 'server:103', 'server:104'])
                self.assertEqual([row.message.create_time for row in records],
                    [1700000010, 1700000020, 1700000025, 1700000030, 1700000040, 1700000050])
                self.assertEqual(records[0].message.local_id, records[1].message.local_id)
                self.assertEqual(records[2].message.content, 'legacy body retained')
                self.assertEqual(records[-1].binding_status, 'archive_only')
                self.assertEqual(len(cursors), 2)
                metadata = json.loads((f.archive / 'capsule_meta.json').read_text(encoding='utf8'))
                backup = Path(metadata['v2_backup'])
                self.assertEqual(file_hashes(backup), original)
                self.assertFalse(backup.is_relative_to(f.archive))
                self.assertEqual(service.list_targets()[0].root, f.archive)
                self.assertEqual((f.archive / 'snapshots/old-synthetic/chat.md').read_text(), 'synthetic preserved snapshot')
                original_archive_id = schema['archive_id']
                asset_inode = (f.archive / 'assets/images/old.png').stat().st_ino
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    repeated = service.reconciliation_backup(f.archive)
                    self.assertEqual(repeated.capsule.new_message_count, 0)
                    service = make_service(root, f)
                    with closing(sqlite3.connect(f.source / 'message/message_1.db')) as connection:
                        connection.execute('INSERT INTO "' + f.table + '" VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (21, 1, 106, 2, 1700000100, 'after restart', b'', b'', None, 800))
                        connection.commit()
                    source = file_hashes(f.source)
                    quick = service.quick_backup(f.archive)
                    self.assertEqual((quick.capsule.message_count, quick.capsule.new_message_count), (7, 1))
                    self.assertEqual(quick.capsule.save_mode, 'quick')
                    repeated = service.reconciliation_backup(f.archive)
                    self.assertEqual(repeated.capsule.new_message_count, 0)
                final_schema = json.loads((f.archive / 'state/schema.json').read_text(encoding='utf8'))
                self.assertEqual(final_schema['archive_id'], original_archive_id)
                self.assertEqual(final_schema['data_generation'], 4)
                self.assertEqual(file_hashes(f.source), source)
                self.assertEqual(file_hashes(backup), original)
                self.assertEqual((f.archive / 'assets/images/old.png').stat().st_ino, asset_inode)
                try:
                    imported = service.import_existing_archive(f.archive)
                except RuntimeError as exc:
                    self.fail('Original service must recognize its reopened v3 archive: ' + str(exc))
                self.assertEqual(imported.root, f.archive)
            finally:
                close_test_logs(root)


if __name__ == '__main__':
    unittest.main()
