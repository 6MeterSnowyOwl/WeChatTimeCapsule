"""Real original-window synthetic acceptance driver; no production test hooks."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QProgressDialog

from tests.one_shot_fixtures import file_hashes
from tests.one_shot_provider_fixtures import PlaintextDescriptor
from tests.full_history_fixtures import make_fixture, make_service, close_test_logs
from wechat_exporter import one_shot_provider
from wechat_exporter.gui.main_window import MainWindow
from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.product_history import read_product


def exercise(root, *, fresh=False, capture=None, avatar_changes=False):
    print('synthetic GUI: preparing fixture', flush=True)
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont('Microsoft YaHei UI', 10))  # Same initialization as original GUI.run.
    from wechat_exporter.gui.theme import apply_application_theme
    apply_application_theme(app)
    f = make_fixture(root)
    if avatar_changes:
        from tests.test_full_history_review import set_avatar_fixture
        from wechat_exporter.integrity import generate_current_manifest
        set_avatar_fixture(f, 'blue')
        meta_path = f.archive / 'capsule_meta.json'
        meta = json.loads(meta_path.read_text(encoding='utf8'))
        meta.update(last_saved_at='2023-11-15T00:00:00+00:00', avatars={
            'contact': dict(path='assets/avatars/contact.png', status='local_database')})
        meta_path.write_text(json.dumps(meta), encoding='utf8')
        generate_current_manifest(f.archive)
    service = make_service(root, f)
    if fresh:
        from wechat_exporter.models import ContactChoice
        target = service.add_target(ContactChoice.from_fields(username='synthetic-conversation', nickname='Synthetic Fresh'))
    else:
        target = service.import_existing_archive(f.archive)
    service.settings.suppress_first_backup_notice(target.archive_id)
    errors, steps = [], []
    def close_dialogs():
        for widget in app.topLevelWidgets():
            if not widget.isVisible():
                continue
            if isinstance(widget, QMessageBox):
                errors.append(widget.detailedText())
                widget.accept()
            elif isinstance(widget, QDialog) and not isinstance(widget, QProgressDialog):
                widget.accept()
    timer = QTimer()
    timer.timeout.connect(close_dialogs)
    timer.start(20)
    window = None
    def wait(predicate):
        deadline = time.monotonic() + 50
        while not predicate():
            app.processEvents()
            if errors:
                raise AssertionError(errors)
            if time.monotonic() > deadline:
                raise AssertionError('synthetic GUI timed out')
            time.sleep(.01)
        app.processEvents()
    def open_window():
        print('synthetic GUI: constructing original MainWindow', flush=True)
        result = MainWindow(service)
        result.show()
        wait(lambda: not result.workers and result.last_wechat_status and bool(result.target_cards))
        print('synthetic GUI: original window ready', flush=True)
        return result
    def click(mode):
        print('synthetic GUI: click ' + mode, flush=True)
        card = window.target_cards[0]
        card.set_expanded(True)
        button = card.reconcile_button if mode == 'full' else card.quick_button
        if not button.isEnabled():
            raise AssertionError('real original save button not enabled')
        QTest.mouseClick(button, Qt.LeftButton)
        wait(lambda: not window.workers and not window.active_mutation)
        summary = service.list_targets()[0]
        bundle, cursors = read_product(summary.root, ArchiveScope('synthetic-stable', 'synthetic-conversation'))
        metadata = json.loads((summary.root / 'capsule_meta.json').read_text(encoding='utf8'))
        steps.append(dict(button=mode, actual_mode=metadata['save_mode'], generation=bundle.data_generation,
            count=len(bundle.records), newly_discovered=metadata['counts']['newly_discovered_count'],
            selected=metadata['counts']['raw_selected_count'], total=metadata['counts']['raw_source_count'],
            cursor_count=len(cursors), integrity=summary.integrity_ok,
            avatar_history_count=metadata.get('avatar_history_count', 0),
            report_count=len(list((summary.root / 'reports').glob('reconciliation_*.json')))))
        print('synthetic GUI result: ' + json.dumps(steps[-1]), flush=True)
        return summary.root
    before = file_hashes(f.source)
    initial_v2 = file_hashes(target.root)
    try:
        with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
            window = open_window()
            output = click('quick' if fresh else 'full')
            metadata = json.loads((output / 'capsule_meta.json').read_text(encoding='utf8'))
            backup = Path(metadata['v2_backup'])
            if file_hashes(backup) != initial_v2 or file_hashes(f.source) != before:
                raise AssertionError('independent v2 backup or source differs')
            if output != target.root:
                raise AssertionError('selected original target moved')
            original, _ = read_product(output, ArchiveScope('synthetic-stable', 'synthetic-conversation'))
            asset_ids = {p.relative_to(output).as_posix(): p.stat().st_ino
                for parent in (output / 'assets', output / 'state/assets') for p in parent.rglob('*') if p.is_file()
                and p.relative_to(output).as_posix().removeprefix('state/') not in {
                    'assets/avatars/me.png', 'assets/avatars/contact.png', 'assets/avatars/avatar_history.json'}}
            window.close()
            service = make_service(root, f)
            if avatar_changes:
                set_avatar_fixture(f, 'green')
            with closing(sqlite3.connect(f.source / 'message/message_1.db')) as connection:
                connection.execute('INSERT INTO "' + f.table + '" VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (21, 1, 106, 2, 1700000100, 'after application restart', b'', b'', None, 800))
                connection.commit()
            # A newly appearing shard must be read fully even in quick mode.
            new_db = f.source / 'message/message_2.db'
            with closing(sqlite3.connect(new_db)) as connection:
                from tests.history_fixtures import _SCHEMA
                connection.execute(_SCHEMA.replace('Msg_synthetic', '"' + f.table + '"'))
                connection.execute('INSERT INTO "' + f.table + '" VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (1, 1, 107, 2, 1699999990, 'new shard historical message', b'', b'', None, 2))
                connection.commit()
            f.runtime.base_db._keys['message/message_2.db'] = b'k' * 32
            before = file_hashes(f.source)
            window = open_window()
            click('quick')
            click('full')
            click('full')
            final, _ = read_product(output, ArchiveScope('synthetic-stable', 'synthetic-conversation'))
            if original.archive_id != final.archive_id or steps[-1]['newly_discovered'] != 0:
                raise AssertionError('identity or idempotence failed')
            if file_hashes(f.source) != before or file_hashes(backup) != initial_v2:
                raise AssertionError('subsequent saves changed source or v2 backup')
            for relative, inode in asset_ids.items():
                if (output / relative).stat().st_ino != inode:
                    raise AssertionError('retained asset was rewritten')
            if capture:
                window.target_cards[0].set_expanded(True)
                app.processEvents()
                if not window.grab().save(str(capture)):
                    raise AssertionError('screenshot failed')
            return dict(steps=steps, output=output.relative_to(root).as_posix(),
                backup=backup.relative_to(root).as_posix(), same_target_root=True,
                files=file_hashes(output), old_target_converted_to_v3=True,
                source_files_unchanged=True, v2_safety_backup_preserved=True,
                v2_backup_matches_original_file_set_and_hashes=True,
                retained_asset_identities_unchanged=True,
                retained_asset_identity_scope='immutable assets; current avatar aliases/history index are mutable',
                avatar_changes_exercised=avatar_changes)
    finally:
        timer.stop()
        if window is not None:
            window.thread_pool.waitForDone(50000)
            window.close()
        close_test_logs(root)


def retain(destination):
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError('demo destination must be new')
    # A retained explicit TemporaryDirectory keeps its synthetic settings and
    # backup references valid at their original paths for local evidence review.
    destination.mkdir(parents=True)
    with TemporaryDirectory(prefix='fixture-', dir=destination, delete=False) as temporary:
        root = Path(temporary)
        result = exercise(root, capture=destination / 'original-gui.png', avatar_changes=True)
        result['fixture_directory'] = root.relative_to(destination).as_posix()
        for key in ('output', 'backup'):
            result[key] = (root.relative_to(destination) / result[key]).as_posix()
        (destination / 'synthetic-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    return result


if __name__ == '__main__':
    import sys
    import faulthandler
    faulthandler.dump_traceback_later(40, repeat=False)
    print(json.dumps(retain(sys.argv[1]), ensure_ascii=False, indent=2))
