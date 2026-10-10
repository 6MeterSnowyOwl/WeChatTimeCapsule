"""Explicit synthetic files for the bounded one-shot subprocess demonstration."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from PIL import Image
from wechat_exporter.archive import ArchivedMessage, write_message_store_atomic
from wechat_exporter.models import ChatMessage
from tests.history_fixtures import _SCHEMA


def file_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def make_fixture(root: Path, *, colliding_archive_asset=False):
    root.mkdir(parents=True, exist_ok=True)
    archive, source, media = root / 'archive-v2', root / 'sources', root / 'media'
    (source / 'message').mkdir(parents=True)
    media.mkdir()
    old = root / 'old.png'
    new = root / 'new.png'
    Image.new('RGB', (2, 2), (255, 40, 70)).save(old)
    Image.new('RGB', (2, 2), (20, 80, 255)).save(new)
    old_bytes, new_bytes = old.read_bytes(), new.read_bytes()
    old.unlink()
    new.unlink()
    digest = hashlib.md5(new_bytes).hexdigest()
    video_bytes = b'synthetic-local-video-payload'
    video_digest = hashlib.md5(video_bytes).hexdigest()
    def row(local, kind, server, stamp, seq, body):
        packed = b'\x00packed:' + body.encode() + b'\xff' if kind in (3, 43) else b''
        return (local, kind, server, 1, stamp, body, b'', packed, None, seq)
    rows = (
        (row(1, 3, 101, 1700000010, 500, 'source image representation'),
         row(2, 1, 105, 1700000025, 101, 'source body differs'),
         row(4, 3, 103, 1700000030, 1, digest)),
        (row(1, 1, 102, 1700000020, 100, 'colliding local ID distinct server'),
         row(9, 3, 103, 1700000030, 1, digest),
         row(5, 43, 104, 1700000040, 2, video_digest)),
    )
    for index, values in enumerate(rows):
        with closing(sqlite3.connect(source / f'message/message_{index}.db')) as connection:
            connection.execute(_SCHEMA)
            connection.executemany('INSERT INTO Msg_synthetic VALUES (?,?,?,?,?,?,?,?,?,?)', values)
            connection.commit()
    cached = media / 'msg/attach' / hashlib.md5(b'synthetic-conversation').hexdigest() / 'synthetic-month' / (digest + '.dat')
    cached.parent.mkdir(parents=True)
    cached.write_bytes(bytes(value ^ 0x88 for value in new_bytes))
    video = media / 'msg/video/synthetic-month' / (video_digest + '.mp4')
    video.parent.mkdir(parents=True)
    video.write_bytes(video_bytes)
    def message(local, kind, stamp, seq, content):
        return ChatMessage(str(local), kind, 1, 'synthetic-sender', stamp, content, seq)
    archive_only = (ArchivedMessage(message(99, '图片', 1700000050, 20, 'archive only retained'),
                    'success', 'server_103_image.png', 'images') if colliding_archive_asset else
                    ArchivedMessage(message(99, '文本', 1700000050, 20, 'archive only retained')))
    records = (
        archive_only,
        ArchivedMessage(message(2, '文本', 1700000025, 101, 'legacy body retained')),
        ArchivedMessage(message(1, '图片', 1700000010, 500, 'legacy image representation'), 'success', 'old.png', 'images'),
    )
    write_message_store_atomic(archive / 'state/messages.jsonl', records)
    (archive / 'state/schema.json').write_text('{"schema_version":2}', encoding='utf8')
    manifest = {'schema_version': 1, 'capsule': 'current', 'files': {}}
    old_assets = [('assets/images/old.png', old_bytes), ('assets/images/unused.png', old_bytes)]
    if colliding_archive_asset:
        old_assets.append(('assets/images/server_103_image.png', old_bytes))
    for relative, payload in old_assets:
        for location in (archive / relative, archive / 'state' / relative):
            location.parent.mkdir(parents=True, exist_ok=True)
            location.write_bytes(payload)
        manifest['files'][relative] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    (archive / 'integrity').mkdir()
    (archive / 'integrity/current.json').write_text(json.dumps(manifest), encoding='utf8')
    source_manifest = root / 'source-manifest.json'
    source_manifest.write_text(json.dumps({'synthetic': True, 'mode': 'full',
        'account_id': 'synthetic-account', 'conversation_id': 'synthetic-conversation',
        'source_root': str(source.absolute()), 'target_table': 'Msg_synthetic',
        'shards': ['message/message_0.db', 'message/message_1.db']}), encoding='utf8')
    return SimpleNamespace(root=root, archive=archive, source=source, media=media,
        manifest=source_manifest, old_bytes=old_bytes, new_bytes=new_bytes, video_bytes=video_bytes)


def cli_args(fixture, output):
    return ['--mode', 'synthetic', '--account', 'synthetic-account',
        '--conversation', 'synthetic-conversation', '--archive', str(fixture.archive),
        '--source-manifest', str(fixture.manifest), '--media-root', str(fixture.media),
        '--output', str(output)]
