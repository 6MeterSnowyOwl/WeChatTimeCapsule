"""Behavioral regressions raised by the first independent whole-change review."""
from datetime import datetime
import json
import io
import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.one_shot_fixtures import file_hashes
from tests.full_history_fixtures import make_fixture, make_service, close_test_logs
from tests.one_shot_provider_fixtures import PlaintextDescriptor
from wechat_exporter import one_shot_provider


def set_avatar_fixture(fixture, color):
    """Real synthetic head_image SQLite; only the existing decrypt boundary is fake."""
    from PIL import Image
    image = io.BytesIO()
    if color is not None:
        Image.new('RGB', (3, 3), color).save(image, format='PNG')
    path = fixture.source / 'head_image/head_image.db'
    path.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS head_image (username TEXT, image_buffer BLOB, update_time INTEGER)')
        connection.execute('DELETE FROM head_image')
        if color is not None:
            connection.executemany('INSERT INTO head_image VALUES (?,?,?)',
                [(name, image.getvalue(), 1) for name in ('synthetic-stable', 'synthetic-conversation', 'synthetic-fresh')])
        connection.commit()
    db = fixture.runtime.base_db
    relative = str(path.relative_to(fixture.source))
    if relative not in db._keys:
        db._keys[relative] = b'k' * 32
        db._db_files.append((relative, str(path), 0))
    # Explicit generated cache is disposable; make same-size updates observable.
    for cache in Path(db.workdir).glob(relative.replace(os.sep, '__') + '*'):
        cache.unlink()
    return image.getvalue()


class ReviewFixTests(unittest.TestCase):
    def test_fresh_avatar_capture_is_rendered_and_durable(self):
        from wechat_exporter.models import ContactChoice
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_fixture(root)
            image = set_avatar_fixture(fixture, 'blue')
            fresh_table = 'Msg_' + hashlib.md5(b'synthetic-fresh').hexdigest()
            with closing(sqlite3.connect(fixture.source / 'message/message_0.db')) as connection:
                connection.execute(f'CREATE TABLE "{fresh_table}" AS SELECT * FROM "{fixture.table}" WHERE 0')
                connection.execute(f'INSERT INTO "{fresh_table}" VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (22, 1, 900, 1, 1700000200, 'synthetic avatar sender', b'', b'', None, 900))
                connection.commit()
            service = make_service(root, fixture)
            try:
                service.settings.data['archive_root'] = str(root / 'fresh')
                target = service.add_target(ContactChoice.from_fields(username='synthetic-fresh', nickname='Synthetic'))
                source = file_hashes(fixture.source)
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    saved = service.reconciliation_backup(target.root)
                self.assertTrue((target.root / 'assets/avatars/contact.png').is_file(), 'Fresh original save must collect local avatar')
                for alias in ('me.png', 'contact.png'):
                    self.assertEqual((target.root / 'assets/avatars' / alias).read_bytes(), image)
                    self.assertEqual((target.root / 'state/assets/avatars' / alias).read_bytes(), image)
                self.assertEqual(saved.capsule.avatar_history_count, 1)
                self.assertEqual(len(service.avatar_history(target.root)), 1)
                self.assertIn('assets/avatars/contact.png', (target.root / 'chat.md').read_text(encoding='utf8'))
                self.assertEqual(source, file_hashes(fixture.source))
            finally:
                close_test_logs(root)

    def test_old_current_avatar_without_index_survives_change_repeat_and_fallback(self):
        from wechat_exporter.integrity import generate_current_manifest
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_fixture(root)
            old = (fixture.archive / 'assets/avatars/contact.png').read_bytes()
            path = fixture.archive / 'capsule_meta.json'
            metadata = json.loads(path.read_text(encoding='utf8'))
            metadata.update(last_saved_at='2023-11-15T00:00:00+00:00', avatars={
                'contact': {'path': 'assets/avatars/contact.png', 'status': 'local_database'}})
            path.write_text(json.dumps(metadata), encoding='utf8')
            generate_current_manifest(fixture.archive)
            self.assertFalse((fixture.archive / 'assets/avatars/avatar_history.json').exists())
            self.assertEqual(list((fixture.archive / 'snapshots').rglob('contact.*')), [])
            blue = set_avatar_fixture(fixture, 'blue')
            service = make_service(root, fixture)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    saved = service.reconciliation_backup(fixture.archive)
                    self.assertEqual(saved.capsule.avatar_history_count, 2, 'Old current alias must be retained as history before replacement')
                    history = service.avatar_history(fixture.archive)
                    self.assertEqual({(fixture.archive / item.path).read_bytes() for item in history}, {old, blue})
                    retained = {item.path: ((fixture.archive / item.path).read_bytes(),
                        (fixture.archive / item.path).stat().st_ino) for item in history}
                    green = set_avatar_fixture(fixture, 'green')
                    previous = file_hashes(fixture.archive)
                    from wechat_exporter import product_history, current_capsule
                    real_history = current_capsule._build_avatar_history
                    def corrupt_old_history(*args, **kwargs):
                        result = real_history(*args, **kwargs)
                        (args[1] / next(iter(retained))).write_bytes(b'synthetic forbidden history overwrite')
                        return result
                    with patch.object(current_capsule, '_build_avatar_history', side_effect=corrupt_old_history):
                        with self.assertRaisesRegex(ValueError, 'IMMUTABLE_ASSET_CONFLICT'):
                            service.quick_backup(fixture.archive)
                    self.assertEqual(file_hashes(fixture.archive), previous)
                    real_read = product_history.read_product
                    def fail_updated_aliases(path, scope):
                        result = real_read(path, scope)
                        if path == fixture.archive and result[0].data_generation == 2:
                            raise ValueError('synthetic avatar readback failure')
                        return result
                    with patch.object(product_history, 'read_product', side_effect=fail_updated_aliases):
                        with self.assertRaisesRegex(ValueError, 'synthetic avatar readback failure'):
                            service.quick_backup(fixture.archive)
                    current_capsule.recover_current_capsule_transaction(fixture.archive)
                    self.assertEqual(file_hashes(fixture.archive), previous)
                    saved = service.quick_backup(fixture.archive)
                    self.assertEqual(saved.capsule.avatar_history_count, 3)
                    self.assertEqual((fixture.archive / 'assets/avatars/contact.png').read_bytes(), green)
                    self.assertEqual((fixture.archive / 'state/assets/avatars/contact.png').read_bytes(), green)
                    repeated = service.reconciliation_backup(fixture.archive)
                    self.assertEqual(repeated.capsule.avatar_history_count, 3)
                    for relative, (data, inode) in retained.items():
                        self.assertEqual((fixture.archive / relative).read_bytes(), data)
                        self.assertEqual((fixture.archive / relative).stat().st_ino, inode)
                        self.assertEqual((fixture.archive / 'state' / relative).read_bytes(), data)
                    set_avatar_fixture(fixture, None)
                    service.quick_backup(fixture.archive)
                    self.assertEqual((fixture.archive / 'assets/avatars/contact.png').read_bytes(), green)
                    self.assertEqual(len(service.avatar_history(fixture.archive)), 3)
                    self.assertTrue(service.verify_capsule(fixture.archive).ok)
            finally:
                close_test_logs(root)

    def test_full_report_contract_uses_actual_core_and_quick_has_no_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_fixture(root)
            service = make_service(root, fixture)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    first = service.reconciliation_backup(fixture.archive)
                    self.assertIsNotNone(first.reconciliation, 'Original full outcome must retain reconciliation result')
                    self.assertTrue(first.report_path.is_file())
                    report = json.loads(first.report_path.read_text(encoding='utf8'))
                    self.assertTrue(report['current_capsule_updated'])
                    self.assertEqual((report['database_message_count'], report['archive_before_count'],
                        report['archive_after_count'], report['newly_discovered']), (5, 3, 6, 3))
                    self.assertEqual(report['matched_existing'], 0)
                    self.assertEqual(report['conflict_count'], 2)
                    self.assertEqual({item['message_identity'] for item in report['conflicts']}, {'server:101', 'server:105'})
                    self.assertTrue(all(item['conflict_fields'] for item in report['conflicts']))
                    self.assertNotIn('legacy body retained', first.report_path.read_text(encoding='utf8'))
                    repeated = service.reconciliation_backup(fixture.archive)
                    self.assertNotEqual(first.report_path, repeated.report_path)
                    self.assertTrue(first.report_path.is_file())
                    self.assertEqual((repeated.reconciliation.newly_discovered, repeated.reconciliation.matched_existing), (0, 3))
                    self.assertEqual(len(repeated.reconciliation.collection.records), 6)
                    self.assertEqual(repeated.reconciliation.collection.source_rows_read, 6)
                    quick = service.quick_backup(fixture.archive)
                    self.assertIsNone(quick.reconciliation)
                    self.assertIsNone(quick.report_path)
            finally:
                close_test_logs(root)


    def test_prepared_copy_and_journal_failures_preserve_current_and_allow_retry(self):
        from wechat_exporter import current_capsule as current
        from wechat_exporter.integrity import generate_current_manifest
        for fault in ('copy', 'journal', 'journal_replace'):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                root = Path(directory)
                target, staging = root / 'target', root / 'staging'
                for path, content in ((target, 'previous'), (staging, 'replacement')):
                    path.mkdir()
                    (path / 'chat.md').write_text(content, encoding='utf8')
                    (path / 'capsule_meta.json').write_text('{}', encoding='utf8')
                    generate_current_manifest(path)
                original = file_hashes(target)
                owner = current.shutil if fault == 'copy' else Path if fault == 'journal_replace' else current
                method = 'copy2' if fault == 'copy' else 'replace' if fault == 'journal_replace' else 'write_json_atomic'
                with patch.object(owner, method, side_effect=OSError('synthetic preparation fault')):
                    with self.assertRaisesRegex(OSError, 'synthetic preparation fault'):
                        current._commit_current_capsule(target, staging, datetime.now().astimezone())
                current.recover_current_capsule_transaction(target)
                self.assertFalse((target / current.CURRENT_TRANSACTION).exists(), 'Preparation debris must be owned and cleared')
                self.assertEqual(file_hashes(target), original, 'Failed preparation must not strand recovery debris')
                current._commit_current_capsule(target, staging, datetime.now().astimezone())
                self.assertEqual((target / 'chat.md').read_text(), 'replacement')

    def test_offline_import_v3_uses_archive_scope_without_account_discovery(self):
        from wechat_exporter.service import ExporterService, AppSettings
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = make_fixture(root)
            service = make_service(root, fixture)
            try:
                with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                    service.reconciliation_backup(fixture.archive)
                original = file_hashes(fixture.archive)
                settings = AppSettings(root / 'offline/settings.json', root / 'offline/exports')
                offline = ExporterService(root / 'offline', settings=settings, account_lister=lambda: [],
                    database_factory=lambda _: self.fail('Offline archive import must not authenticate'))
                try:
                    imported = offline.import_existing_archive(fixture.archive)
                except (RuntimeError, ValueError) as exc:
                    self.fail('Strict saved archive must be importable offline: ' + str(exc))
                self.assertEqual(imported.root, fixture.archive)
                self.assertTrue(imported.integrity_ok)
                self.assertEqual(file_hashes(fixture.archive), original)
            finally:
                close_test_logs(root)
