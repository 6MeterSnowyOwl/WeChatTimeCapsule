"""Spawn-safe plaintext fixture; only the encrypted-file boundary is replaced."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

from tests.one_shot_fixtures import make_fixture
from wechat_exporter.history_provider import AuthenticatedHistoryDB


def fixture(root):
    f = make_fixture(root)
    account = root / 'account'
    account.mkdir()
    shutil.move(str(f.source), account / 'db_storage')
    shutil.move(str(f.media / 'msg'), account / 'msg')
    f.source, f.media = account / 'db_storage', account
    f.work = root.parent / 'work'
    f.work.mkdir()
    table = 'Msg_' + hashlib.md5(b'synthetic-conversation').hexdigest()
    for path in (f.source / 'message').iterdir():
        with closing(sqlite3.connect(path)) as c:
            c.execute('ALTER TABLE Msg_synthetic RENAME TO "' + table + '"')
            c.execute('UPDATE "' + table + '" SET real_sender_id=2')
            c.commit()
    from wechatauto.db import WeChatDB
    base = object.__new__(WeChatDB)
    base.__dict__.update(account='synthetic-account', account_dir=str(account), workdir=str(root.parent / 'unused-shared-cache'),
        _keys={f'message/message_{i}.db': b'k' * 32 for i in range(2)}, _db_files=[])
    f.runtime = AuthenticatedHistoryDB(base, 'synthetic-account')
    f.table = table
    return f


def kwargs(f, output):
    return dict(mode='real', account='synthetic-account', conversation='synthetic-conversation',
        archive=f.archive, source_manifest=None, media_root=f.media, output=output,
        authenticated_db=f.runtime, source_root=f.source,
        expected_shards=('message/message_0.db', 'message/message_1.db'), metadata_paths=(),
        image_domain=f.media / 'msg/attach' / hashlib.md5(b'synthetic-conversation').hexdigest(),
        video_domain=f.media / 'msg/video', work_root=f.work,
        origin_confirmed=True, real_read_confirmed=True)


def descriptor_type():
    # Loaded only after production module exists; importing this fixture cannot
    # make a missing production bridge RED fail with ImportError.
    from wechat_exporter.one_shot_provider import RealDescriptor
    return PlaintextDescriptor


try:
    from wechat_exporter.one_shot_provider import RealDescriptor
except ImportError:
    RealDescriptor = object


@dataclass(frozen=True)
class PlaintextDescriptor(RealDescriptor):
    def build(self, workdir, *, verification=False):
        provider = super().build(workdir, verification=verification)
        provider._decrypt_file = lambda source, destination, key: shutil.copyfile(source, destination)
        return provider


def retain_proof(destination):
    """Produce a retained synthetic artifact through the programmatic CLI."""
    from contextlib import redirect_stdout
    import io
    from unittest.mock import patch
    from scripts.export_full_history_once import main
    from wechat_exporter import one_shot_provider
    from wechat_exporter.integrity import verify_current_capsule
    from tests.one_shot_fixtures import file_hashes
    root = Path(destination).absolute()
    root.mkdir(exist_ok=False)
    f = fixture(root / 'input')
    before = file_hashes(f.root)
    output = root / 'capsule'
    options = kwargs(f, output)
    arguments = []
    for key in ('mode','account','conversation','archive','source_manifest','media_root','output'):
        arguments.extend(('--'+key.replace('_','-'), str(options.pop(key))))
    with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor), redirect_stdout(io.StringIO()) as stream:
        status = main(arguments, **options)
    if status != 0: raise AssertionError(stream.getvalue())
    metadata = json.loads((output/'capsule_meta.json').read_bytes())
    assets = sorted(str(p.relative_to(output)).replace('\\','/') for p in (output/'assets').rglob('*') if p.is_file())
    return dict(cli_exit=status, result=json.loads(stream.getvalue()), assets=assets,
        coverage=metadata['coverage'], counts=metadata['counts'],
        first_message_time=metadata['first_message_time'], last_message_time=metadata['last_message_time'],
        integrity_ok=verify_current_capsule(output).ok,
        inputs_unchanged=before == file_hashes(f.root), work_empty=list(f.work.iterdir()) == [])


if __name__ == '__main__':
    import sys
    print(json.dumps(retain_proof(sys.argv[1]), ensure_ascii=False))
