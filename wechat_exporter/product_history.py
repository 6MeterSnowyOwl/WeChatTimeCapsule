"""Original product lifecycle around the shared historical Core writer.

No account discovery/authentication, activation, or authority machinery lives
here. The service supplies its actual authenticated runtime and scoped target.
"""
from dataclasses import asdict, replace
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from .current_capsule import _commit_current_capsule, recover_current_capsule_transaction
from .history_contracts import ArchiveScope, SourceCursor, SourceKey
from .history_provider import require_plain_path
from .integrity import current_capsule_file_facts, sha256_file
from .locks import TargetOperationLock
from .one_shot_export import _export_prepared, _tree_facts, _validate_output
from .one_shot_provider import overlaps, selected_source_facts
from .source_media import validate_archived_assets
from .v3_state import decode_state, _load_json


_MUTABLE_AVATAR_LEAVES = frozenset(
    ['assets/avatars/avatar_history.json'] +
    [f'assets/avatars/{role}{suffix}' for role in ('me', 'contact')
     for suffix in ('.jpg', '.jpeg', '.png', '.webp', '.gif')])


def _json(path):
    return _load_json(path.read_bytes())


def read_product(root, scope):
    """Strict complete readback; every required state/vault leaf is hash-covered."""
    require_plain_path(root)
    for path in root.rglob('*'):
        require_plain_path(path)
    schema = _json(root / 'state/schema.json')
    lines = (root / 'state/messages.jsonl').read_bytes().splitlines()
    rows = [_load_json(line) for line in lines]
    bundle = decode_state(json.dumps({'schema': schema, 'records': rows}).encode(), scope)
    target, metadata = _json(root / 'target.json'), _json(root / 'capsule_meta.json')
    if (target.get('scope') != asdict(scope) or target.get('archive_id') != bundle.archive_id
            or target.get('username') != scope.conversation_id
            or target.get('product_history_version') != 1
            or metadata.get('product_history_version') != 1
            or metadata.get('archive_id') != bundle.archive_id
            or type(metadata.get('data_generation')) is not int
            or metadata['data_generation'] != bundle.data_generation):
        raise ValueError('PRODUCT_SCOPE_MISMATCH')
    raw = _json(root / 'state/source_cursors.json')
    if (set(raw) != {'version', 'scope', 'archive_id', 'data_generation', 'cursors'}
            or type(raw['version']) is not int or raw['version'] != 1
            or raw['scope'] != asdict(scope) or raw['archive_id'] != bundle.archive_id
            or type(raw['data_generation']) is not int or raw['data_generation'] != bundle.data_generation
            or type(raw['cursors']) is not list):
        raise ValueError('INVALID_PRODUCT_CURSORS')
    cursors = []
    for item in raw['cursors']:
        if (type(item) is not dict or set(item) != {'source_key', 'high_watermark', 'overlap_seq'}
                or type(item['source_key']) is not dict
                or set(item['source_key']) != {'relative_db', 'incarnation'}
                or type(item['high_watermark']) is not list):
            raise ValueError('INVALID_PRODUCT_CURSORS')
        cursors.append(SourceCursor(SourceKey(**item['source_key']), tuple(item['high_watermark']), item['overlap_seq']))
    if len({item.source_key for item in cursors}) != len(cursors):
        raise ValueError('INVALID_PRODUCT_CURSORS')
    records = bundle.to_core_records()
    _validate_output(root, scope, records)
    manifest = _json(root / 'integrity/current.json')
    facts = current_capsule_file_facts(root)
    if manifest.get('files') != facts:
        raise ValueError('PRODUCT_MANIFEST_INCOMPLETE')
    expected = {path.relative_to(root).as_posix(): sha256_file(path)
        for path in (root / 'state').rglob('*') if path.is_file()}
    if metadata.get('state_hashes') != expected:
        raise ValueError('PRODUCT_STATE_HASHES_INCOMPLETE')
    validate_archived_assets(root, records, manifest)
    return bundle, tuple(cursors)


def _file_hashes(root, *, owned_lock=None):
    """Exact explicit-tree file content facts; legitimate v2 hardlinks are readable."""
    facts = {}
    require_plain_path(root)
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            require_plain_path(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == '.operation.lock' and owned_lock is not None:
                if _json(path).get('operation_id') != owned_lock.operation_id:
                    raise ValueError('TARGET_LOCK_OWNERSHIP_CHANGED')
                continue
            facts[relative] = sha256_file(path)
    return facts


def _preserve_v2(root, owned_lock):
    before = _file_hashes(root, owned_lock=owned_lock)
    parent = root.parent / '.history-backups'
    ancestor = parent if parent.exists() else root.parent
    require_plain_path(ancestor)
    backup = parent / uuid4().hex / 'archive'
    backup.mkdir(parents=True)
    # Copy each leaf, never link it: backup must be physically independent.
    for directory, directories, files in os.walk(root, followlinks=False):
        relative_directory = Path(directory).relative_to(root)
        (backup / relative_directory).mkdir(exist_ok=True)
        for name in files:
            relative = (relative_directory / name).as_posix()
            if relative in before:
                shutil.copy2(root / relative, backup / relative)
    if (_file_hashes(backup) != before
            or _file_hashes(root, owned_lock=owned_lock) != before):
        raise ValueError('V2_BACKUP_VERIFICATION_FAILED')
    _tree_facts(backup)  # Independent file identities, no symlinks/shared hardlinks.
    (backup.parent / 'verified.json').write_text(
        json.dumps(dict(version=1, source=str(root), hashes=before), indent=2), encoding='utf8')
    return backup, before


def _fresh_target(root):
    """Only the exact empty shape emitted by original create_target is fresh."""
    allowed = {'target.json', 'snapshot_index.json', 'archive_state.json', 'state/schema.json',
               'state/messages.jsonl', '.operation.lock'}
    files = _file_hashes(root)
    index, state = _json(root / 'snapshot_index.json'), _json(root / 'archive_state.json')
    if (set(files) - allowed or index.get('snapshots') != []
            or state.get('message_count') != 0 or state.get('last_snapshot_id') is not None
            or state.get('last_sort_seq', 0) != 0):
        return False
    messages = root / 'state/messages.jsonl'
    return not messages.exists() or messages.read_bytes() == b''


def validate_target(root, scope):
    """Recover the selected target before format dispatch, then enforce scope."""
    require_plain_path(root)
    target = _json(root / 'target.json')
    if target.get('username') != scope.conversation_id or (
            target.get('scope') is not None and target['scope'] != asdict(scope)):
        raise ValueError('PRODUCT_SCOPE_MISMATCH')
    if (root / 'current_capsule_journal.json').exists():
        with TargetOperationLock(root, 'recover_history'):
            recover_current_capsule_transaction(root)
    schema = _json(root / 'state/schema.json')
    if type(schema.get('schema_version')) is not int or schema['schema_version'] not in (2, 3):
        raise ValueError('UNSUPPORTED_HISTORY_SCHEMA')
    if schema['schema_version'] == 3:
        read_product(root, scope)
    else:
        _file_hashes(root)
        if not (root / 'state/messages.jsonl').is_file() and not _fresh_target(root):
            raise ValueError('CORRUPT_MISSING_MESSAGE_STATE')
    return schema


def save_product(*, descriptor, media_db, domains, scope, contact, output, work_root, mode,
                 collection_ready=None, capture_avatars=None):
    """Save into the original selected target, after verified v2 preservation."""
    if mode not in ('full', 'quick') or contact.username != scope.conversation_id:
        raise ValueError('PRODUCT_SCOPE_MISMATCH')
    output, work_root = Path(output).absolute(), Path(work_root).absolute()
    protected = [descriptor.storage_root.parent]
    if descriptor.shared_cache_root is not None:
        protected.append(Path(descriptor.shared_cache_root).absolute())
    for path in (output, work_root, *protected):
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        require_plain_path(ancestor)
    if overlaps(output, work_root) or any(overlaps(write, source)
            for write in (output, work_root) for source in protected):
        raise ValueError('OVERLAPPING_ROOTS')
    validate_target(output, scope)
    work_root.mkdir(parents=True, exist_ok=True)
    with TargetOperationLock(output, 'history_' + mode) as owned_lock:
        recover_current_capsule_transaction(output)
        schema = _json(output / 'state/schema.json')
        archived_records, cursors, generation = None, (), 1
        if schema['schema_version'] == 3:
            bundle, cursors = read_product(output, scope)
            archive, archived_records = output, bundle.to_core_records()
            archive_id, generation = bundle.archive_id, bundle.data_generation + 1
            old_meta = _json(output / 'capsule_meta.json')
            backup_path, backup_hashes = old_meta.get('v2_backup'), old_meta.get('v2_backup_hashes')
        else:
            fresh = _fresh_target(output)
            if not fresh:
                from .archive import load_message_store
                archived_records = tuple(load_message_store(output / 'state/messages.jsonl'))
                validate_archived_assets(output, archived_records, _json(output / 'integrity/current.json'))
            backup, backup_hashes = _preserve_v2(output, owned_lock)
            backup_path = str(backup)
            archive = None if fresh else backup
            archived_records = () if fresh else archived_records
            archive_id, mode = uuid4().hex, 'full'
            old_meta = _json(backup / 'capsule_meta.json') if (backup / 'capsule_meta.json').is_file() else {}
        # Keep original avatars, unused assets and additional state leaves.
        retained_root = output if schema['schema_version'] == 3 else Path(backup_path)
        prior_target = _json(retained_root / 'target.json')
        from .models import AvatarSnapshot
        from .archive import ArchiveTarget, AssetCounts
        from .current_capsule import _build_avatar_history
        from datetime import datetime
        previous_avatars = {role: AvatarSnapshot(status=value.get('status', ''),
            relative_path=value.get('path') or None, remote_available=value.get('remote_available', False))
            for role, value in old_meta.get('avatars', {}).items()}
        avatars = dict(previous_avatars)
        avatar_history = []
        def prepare(staging):
            for folder in ('assets', 'state'):
                for path in (retained_root / folder).rglob('*'):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(retained_root)
                    if relative.as_posix() in ('state/schema.json', 'state/messages.jsonl', 'state/source_cursors.json'):
                        continue
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
            if capture_avatars is not None:
                captured = capture_avatars(staging)
                # A temporarily unavailable self avatar retains its last known
                # local snapshot; contact fallback is handled by existing history.
                if not captured['self'].relative_path and previous_avatars.get('self'):
                    captured['self'] = previous_avatars['self']
                captured, entries = _build_avatar_history(
                    ArchiveTarget(retained_root, contact.username, contact.display_name),
                    staging, captured, datetime.now().astimezone(), AssetCounts(),
                    previous_contact=previous_avatars.get('contact'),
                    previous_observed_at=old_meta.get('last_saved_at'), independent_copies=True)
                avatars.clear()
                avatars.update(captured)
                avatar_history.extend(entries)
        def input_facts():
            return (_tree_facts(archive) if archive is not None and archive != output else {},
                    selected_source_facts(descriptor))
        baseline = input_facts()
        def finish(staging, bundle, next_cursors, now):
            target_document = {**prior_target, 'product_history_version': 1, 'scope': asdict(scope),
                'archive_id': bundle.archive_id, 'username': contact.username, 'display_name': contact.display_name}
            (staging / 'target.json').write_text(json.dumps(target_document, ensure_ascii=False), encoding='utf8')
            cursor_document = dict(version=1, scope=asdict(scope), archive_id=bundle.archive_id,
                data_generation=bundle.data_generation, cursors=[asdict(cursor) for cursor in next_cursors])
            (staging / 'state/source_cursors.json').write_text(json.dumps(cursor_document), encoding='utf8')
            metadata = _json(staging / 'capsule_meta.json')
            for key in ('avatars', 'avatar_history_count'):
                if key in old_meta:
                    metadata[key] = old_meta[key]
            if capture_avatars is not None:
                metadata['avatars'] = {role: dict(status=value.status, path=value.relative_path,
                    remote_available=value.remote_available) for role, value in avatars.items()}
                metadata['avatar_history_count'] = len(avatar_history)
            metadata.update(product_history_version=1, archive_id=bundle.archive_id,
                data_generation=bundle.data_generation, last_saved_at=now.isoformat(), save_mode=mode,
                last_save_mode='reconciliation' if mode == 'full' else 'quick',
                v2_backup=backup_path, v2_backup_hashes=backup_hashes,
                state_hashes={p.relative_to(staging).as_posix(): sha256_file(p)
                    for p in sorted((staging / 'state').rglob('*')) if p.is_file()})
            (staging / 'capsule_meta.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf8')
        def publish(staging, now, records):
            read_product(staging, scope)
            for folder in ('assets', 'state/assets'):
                for path in (staging / folder).rglob('*'):
                    if path.is_file():
                        previous = output / path.relative_to(staging)
                        leaf = path.relative_to(staging).as_posix()
                        mutable_avatar = leaf.removeprefix('state/') in _MUTABLE_AVATAR_LEAVES
                        if previous.exists() and not mutable_avatar and sha256_file(previous) != sha256_file(path):
                            raise ValueError('IMMUTABLE_ASSET_CONFLICT')
            def validate(root):
                saved, _ = read_product(root, scope)
                if saved.archive_id != archive_id or saved.data_generation != generation or saved.to_core_records() != records:
                    raise ValueError('PRODUCT_READBACK_MISMATCH')
            _commit_current_capsule(output, staging, now, validate=validate)
            validate(output)
        return _export_prepared(mode='real', account=scope.account_id,
            conversation=scope.conversation_id, archive=archive, source=descriptor.storage_root,
            media=descriptor.storage_root.parent, output=output, descriptor=descriptor,
            media_db=media_db, domains=domains, independent_work=work_root,
            baseline=baseline, input_facts=input_facts,
            validate_roots=lambda: require_plain_path(output), shards=list(descriptor.expected_shards),
            manifest_digest=None, contact_choice=contact, archived_records=archived_records,
            collection_mode=mode, cursors=cursors, archive_id=archive_id, generation=generation,
            finish_staging=finish, publish=publish, prepare_staging=prepare, avatars=avatars,
            collection_ready=collection_ready)
