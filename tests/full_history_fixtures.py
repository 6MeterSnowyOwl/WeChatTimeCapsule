"""Explicit synthetic authenticated account boundary for original RC6 service."""
import json
import os
from pathlib import Path
import shutil

from tests.one_shot_provider_fixtures import fixture
from tests.test_product_history import close_test_logs
from wechat_exporter.account import AccountChoice, BackendInfo
from wechat_exporter.compatibility import WeChatCompatibility, CompatibilityStatus, CapabilityProbeResult, RuntimeCapability
from wechat_exporter.integrity import generate_current_manifest
from wechat_exporter.service import AppSettings, ExporterService


def make_fixture(root):
    f = fixture(root / 'input')
    db = f.runtime.base_db
    Path(db.workdir).mkdir()
    db._db_files = [(name.replace('/', os.sep), str(f.source / name), 0) for name in sorted(db._keys)]
    db._keys = {name.replace('/', os.sep): value for name, value in db._keys.items()}
    db._decrypt_file = lambda source, destination, key: shutil.copyfile(source, destination)
    target = f.archive
    for name, document in {
        'target.json': dict(schema_version=2, username='synthetic-conversation', display_name='Synthetic History'),
        'archive_state.json': dict(schema_version=2, message_count=3, last_sort_seq=500),
        'snapshot_index.json': dict(schema_version=2, snapshots=[dict(id='old-synthetic', status='COMPLETE')]),
        'capsule_meta.json': dict(capsule_schema_version=1, integrity_status='complete', message_count=3),
    }.items():
        (target / name).write_text(json.dumps(document), encoding='utf8')
    (target / 'chat.md').write_text('# Synthetic original v2\n![old](assets/images/old.png)\n', encoding='utf8')
    snapshot = target / 'snapshots/old-synthetic'
    snapshot.mkdir(parents=True)
    (snapshot / 'chat.md').write_text('synthetic preserved snapshot', encoding='utf8')
    # Valid original assets may be hardlinked between current, durable and snapshots.
    durable = target / 'state/assets/images/old.png'
    durable.unlink()
    os.link(target / 'assets/images/old.png', durable)
    os.link(target / 'assets/images/old.png', snapshot / 'old.png')
    (target / 'state/extra-legacy.json').write_text('{"retain":true}', encoding='utf8')
    avatars = target / 'assets/avatars'
    avatars.mkdir()
    shutil.copyfile(target / 'assets/images/old.png', avatars / 'contact.png')
    generate_current_manifest(target)
    return f


def make_service(root, f):
    settings_path = root / 'application/settings.json'
    if not settings_path.exists():
        settings_path.parent.mkdir()
        settings_path.write_text(json.dumps(dict(archive_root=str(root / 'input'), known_archive_roots=[],
            selected_account_identity='synthetic-stable', onboarding_completed=True)), encoding='utf8')
    settings = AppSettings(settings_path, root / 'unused-exports')
    account = AccountChoice('synthetic-stable', 'synthetic-account', 'Synthetic Account', 'test', 1)
    return ExporterService(root, settings=settings, account_lister=lambda: [account],
        database_factory=lambda selected: (f.runtime.base_db, BackendInfo('1.2.1', 'synthetic-account', 'synthetic-stable')),
        compatibility_provider=lambda: WeChatCompatibility(True, '4.1.13.63', CompatibilityStatus.TESTED, 'Synthetic'),
        probe_runner=lambda selected: CapabilityProbeResult(RuntimeCapability.READY))
