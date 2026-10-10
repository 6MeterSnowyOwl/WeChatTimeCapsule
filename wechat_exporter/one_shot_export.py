"""Explicit one-shot composition of the existing historical Core."""
from dataclasses import asdict, replace
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from .archive import load_message_store, media_relative_path
from .conversation_reader import ConversationReader
from .history_collection import collect_history
from .history_contracts import ArchiveScope, HistoryError, ScopeAuthorization
from .history_provider import require_plain_path
from .integrity import generate_current_manifest, sha256_file, verify_current_capsule
from .markdown import MarkdownArchiveWriter, sender_resolver
from .models import ContactChoice, ExportStats, MediaResult
from .message_types import coverage_bucket
from .one_shot_sqlite import SyntheticSQLiteDescriptor
from .source_media import export_record_media, validate_archived_assets
from .track2_contracts import StateBundle
from .v3_state import decode_state, encode_messages, encode_schema


def _tree_facts(root):
    """Walk only an explicitly supplied input tree and reject writable aliases."""
    require_plain_path(root)
    facts = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(directory) / name
            require_plain_path(path)
            if path.is_file():
                info = path.stat()
                if info.st_nlink != 1:
                    raise ValueError('SHARED_FILE_IDENTITY')
                facts[path.relative_to(root).as_posix()] = (info.st_dev, info.st_ino, info.st_size, sha256_file(path))
    return facts


def _roots(archive, source, media, manifest, output):
    protected = tuple(root for root in (archive, source, media) if root is not None)
    for path in (*protected, manifest, output.parent):
        require_plain_path(path)
    if not all(root.is_dir() for root in protected) or not manifest.is_file() or not output.parent.is_dir():
        raise ValueError('INVALID_EXPLICIT_ROOT')
    if output.exists() or output.is_symlink():
        raise ValueError('OUTPUT_ALREADY_EXISTS')
    roots = (*protected, output)
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError('OVERLAPPING_ROOTS')
    if output == manifest or output.is_relative_to(manifest) or manifest.is_relative_to(output):
        raise ValueError('OVERLAPPING_ROOTS')


def _validate_output(root, scope, expected):
    schema = json.loads((root / 'state/schema.json').read_bytes())
    rows = [json.loads(line) for line in (root / 'state/messages.jsonl').read_text(encoding='utf8').splitlines()]
    bundle = decode_state(json.dumps({'schema': schema, 'records': rows}, ensure_ascii=False).encode(), scope)
    if bundle.to_core_records() != expected:
        raise ValueError('OUTPUT_RECORD_READBACK_FAILED')
    metadata = json.loads((root / 'capsule_meta.json').read_bytes())
    if metadata['scope'] != asdict(scope) or metadata['message_count'] != len(expected):
        raise ValueError('OUTPUT_SCOPE_COUNT_FAILED')
    for relative, digest in metadata['state_hashes'].items():
        if sha256_file(root / relative) != digest:
            raise ValueError('OUTPUT_STATE_HASH_FAILED')
    text = (root / 'chat.md').read_text(encoding='utf8')
    references = set()
    pattern = r'(?:\[[^\]]*\]\(([^)]+)\)|(?:src|href)="([^"]+)")'
    for match in re.finditer(pattern, text):
        reference = match.group(1) or match.group(2)
        if re.match(r'^https?://', reference):
            continue
        candidate = (root / reference).resolve()
        if (re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', reference) or
                Path(reference).is_absolute() or not candidate.is_relative_to(root.resolve()) or
                not candidate.is_file() or candidate.stat().st_size <= 0):
            raise ValueError('INVALID_LOCAL_REFERENCE')
        require_plain_path(candidate)
        references.add(candidate.relative_to(root.resolve()).as_posix())
    assets = {p.relative_to(root).as_posix() for p in (root / 'assets').rglob('*') if p.is_file()} if (root / 'assets').exists() else set()
    referenced_assets = {ref for ref in references if ref.startswith('assets/')}
    if (not referenced_assets.issubset(assets) or
            (metadata.get('product_history_version') != 1 and assets != referenced_assets)):
        raise ValueError('UNREFERENCED_ASSET')
    integrity = verify_current_capsule(root)
    if not (integrity.ok and integrity.manifest_present):
        raise ValueError('OUTPUT_INTEGRITY_FAILED')


def export_full_history_once(*, mode, account, conversation, archive, source_manifest, media_root, output,
        authenticated_db=None, source_root=None, expected_shards=None, metadata_paths=(),
        image_domain=None, video_domain=None, work_root=None, origin_confirmed=False,
        real_read_confirmed=False, contact_choice=None, reader_deadline_seconds=120):
    if (contact_choice is not None and (not isinstance(contact_choice, ContactChoice)
            or contact_choice.username != conversation or not contact_choice.display_name)):
        raise ValueError('IDENTITY_SCOPE_MISMATCH')
    if (type(reader_deadline_seconds) not in (int, float)
            or not 0 < reader_deadline_seconds <= 1800):
        raise ValueError('INVALID_HISTORY_READER_OPTIONS')
    real = mode == 'real'
    if mode not in ('synthetic', 'real') or (real and authenticated_db is None):
        raise ValueError('REAL_PROVIDER_UNAVAILABLE')
    descriptor = media_db = domains = independent_work = None
    if real:
        from .one_shot_provider import (absolute, select_real_provider, validate_real_roots,
            selected_source_facts, bounded_downloader_factory)
        # The selection checks inert in-memory binding and operation confirmation
        # before any lstat/resolve/read on the selected roots.
        descriptor, media_db, domains = select_real_provider(authenticated_db=authenticated_db,
            account=account, conversation=conversation, source_root=source_root, media_root=media_root,
            expected_shards=expected_shards, metadata_paths=metadata_paths, image_domain=image_domain,
            video_domain=video_domain, origin_confirmed=origin_confirmed, real_read_confirmed=real_read_confirmed)
        archive, source, media, output, independent_work = [absolute(value)
            for value in (archive, source_root, media_root, output, work_root)]
        def validate_roots():
            validate_real_roots(archive, source, media, domains, independent_work, output, descriptor)
        validate_roots()
        def input_facts():
            return (_tree_facts(archive), selected_source_facts(descriptor))
        baseline = input_facts()
        manifest_path, manifest_digest = None, None
        shards = list(descriptor.expected_shards)
    else:
        return _export_synthetic_prepared(account=account, conversation=conversation, archive=archive,
            source_manifest=source_manifest, media_root=media_root, output=output,
            contact_choice=contact_choice, reader_deadline_seconds=reader_deadline_seconds)
    return _export_prepared(mode=mode, account=account, conversation=conversation,
        archive=archive, source=source, media=media, output=output, descriptor=descriptor,
        media_db=media_db, domains=domains, independent_work=independent_work,
        baseline=baseline, input_facts=input_facts, validate_roots=validate_roots,
        shards=shards, manifest_digest=manifest_digest, contact_choice=contact_choice,
        reader_deadline_seconds=reader_deadline_seconds)


def _export_synthetic_prepared(*, account, conversation, archive, source_manifest, media_root, output,
                               contact_choice=None, reader_deadline_seconds=120):
    # Check original ancestors before resolve, so resolution cannot hide links.
    paths = [Path(value).absolute() if value is not None else None
             for value in (archive, source_manifest, media_root, output)]
    for path in paths:
        if path is None:
            continue
        try:
            path.lstat()  # lexistence includes broken links and reparse points.
        except FileNotFoundError:
            require_plain_path(path.parent)
        else:
            require_plain_path(path)
    archive, manifest_path, media, output = [path.resolve() if path is not None else None for path in paths]
    declaration = json.loads(manifest_path.read_text(encoding='utf8'))
    if (declaration.get('synthetic') is not True or declaration.get('mode') != 'full' or
            declaration.get('account_id') != account or declaration.get('conversation_id') != conversation or
            declaration.get('target_table') != 'Msg_synthetic'):
        raise ValueError('INVALID_SYNTHETIC_DECLARATION')
    source = Path(declaration['source_root'])
    if not source.is_absolute():
        raise ValueError('INVALID_EXPLICIT_ROOT')
    require_plain_path(source)
    source = source.resolve(strict=True)
    shards = declaration.get('shards')
    if (type(shards) is not list or not shards or any(type(item) is not str or
            re.fullmatch(r'message/message_[0-9]+\.db', item) is None for item in shards) or
            shards != sorted(set(shards))):
        raise ValueError('INVALID_SYNTHETIC_DECLARATION')
    _roots(archive, source, media, manifest_path, output)
    baseline = tuple(_tree_facts(root) if root is not None else {} for root in (archive, source, media))
    identities = [value[:2] for facts in baseline for value in facts.values()]
    if len(set(identities)) != len(identities) or manifest_path.stat().st_nlink != 1:
        raise ValueError('SHARED_FILE_IDENTITY')
    manifest_digest = sha256_file(manifest_path)
    descriptor = SyntheticSQLiteDescriptor(source, account, 'Msg_synthetic', tuple(shards))
    def input_facts(): return tuple(_tree_facts(root) if root is not None else {} for root in (archive, source, media))
    def validate_roots():
        if sha256_file(manifest_path) != manifest_digest: raise ValueError('INPUT_CHANGED')
        _roots(archive, source, media, manifest_path, output)
    return _export_prepared(mode='synthetic', account=account, conversation=conversation,
        archive=archive, source=source, media=media, output=output, descriptor=descriptor,
        media_db=None, domains=None, independent_work=output.parent, baseline=baseline,
        input_facts=input_facts, validate_roots=validate_roots, shards=shards, manifest_digest=manifest_digest,
        contact_choice=contact_choice, reader_deadline_seconds=reader_deadline_seconds)


def _export_prepared(*, mode, account, conversation, archive, source, media, output,
        descriptor, media_db, domains, independent_work, baseline, input_facts,
        validate_roots, shards, manifest_digest, contact_choice=None, reader_deadline_seconds=120,
        archived_records=None, collection_mode='full', cursors=(), archive_id=None,
        generation=1, finish_staging=None, publish=None, prepare_staging=None, avatars=None,
        collection_ready=None):
    identities = [value[:2] for facts in baseline for value in facts.values()]
    if len(set(identities)) != len(identities): raise ValueError('SHARED_FILE_IDENTITY')
    if archived_records is not None:
        archived = tuple(archived_records)
    elif archive is None:
        archived = ()
    else:
        if json.loads((archive / 'state/schema.json').read_bytes()).get('schema_version') != 2:
            raise ValueError('UNSUPPORTED_INPUT_V2_SCHEMA')
        archived = tuple(load_message_store(archive / 'state/messages.jsonl'))
    old_manifest = json.loads((archive / 'integrity/current.json').read_bytes()) if archive is not None else None
    if archive is not None:
        validate_archived_assets(archive, archived, old_manifest)
    scope = ArchiveScope(account, conversation)
    provider = descriptor.build(independent_work)
    try:
        provider.list_sources()  # Completeness before staging creation.
    finally:
        provider.close()
    now = datetime.now().astimezone()
    with TemporaryDirectory(prefix='.one-shot-', dir=independent_work) as temporary:
        operation = Path(temporary)
        work, staging = operation / 'work', operation / 'capsule'
        work.mkdir()
        staging.mkdir()
        if prepare_staging is not None:
            prepare_staging(staging)
        db = media_db or SimpleNamespace(account_dir=str(media))
        db._history_provider_descriptor, db.workdir = descriptor, str(work)
        downloader_factory = None
        selected_media_facts = {}
        if mode == 'real':
            from .one_shot_provider import bounded_downloader_factory
            downloader_factory = bounded_downloader_factory(domains, work, media_db, selected_media_facts)
        media_counts = {kind: dict(recovered=0, missing=0, reused=0) for kind in ('images', 'videos')}
        with ConversationReader(db, scope, work, deadline_seconds=reader_deadline_seconds).open_inventory(mode=collection_mode, cursors=cursors) as session:
            collection = collect_history(archived, session, mode=collection_mode, authorization=ScopeAuthorization(scope, True))
            if collection_ready is not None:
                collection_ready(collection)
            by_source = {count.source_key: count for count in collection.inventory.counts}
            shard_report = [dict(ordinal=index + 1, raw_total=by_source[source.key].raw_total,
                raw_selected=by_source[source.key].raw_selected, enumerated=by_source[source.key].enumerated,
                decoded=by_source[source.key].decoded, target_present=source.target_present, completion='complete')
                for index, source in enumerate(collection.inventory.sources)]
            source_types = dict(Counter(row.message.message_type for row in collection.inventory.rows))
            # Reserve every retained legacy filename before new Core media
            # installation; Core's exclusive writer then chooses a free suffix.
            preserved = set()
            for record in collection.records:
                relative = media_relative_path(record)
                if relative is not None and relative not in preserved:
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(archive / relative, destination)
                    preserved.add(relative)
            records, recovered, missing = [], 0, 0
            for record in collection.records:
                if record.message.message_type not in ('图片', '视频'):
                    records.append(record)
                    continue
                result = export_record_media(session, record, db, staging, archived_root=archive,
                    manifest=old_manifest, downloader_factory=downloader_factory)
                kind = {'图片': 'images', '视频': 'videos'}[record.message.message_type]
                if result.relative_path:
                    recovered += 1
                    media_counts[kind]['recovered'] += 1
                    if media_relative_path(record) is not None:
                        media_counts[kind]['reused'] += 1
                else:
                    missing += 1
                    media_counts[kind]['missing'] += 1
                records.append(replace(record, media_status=result.status,
                    media_asset=Path(result.relative_path).name if result.relative_path else '',
                    media_kind={'图片': 'images', '视频': 'videos'}[record.message.message_type]))
            session.assert_source_set_stable()
        records = tuple(records)
        counts = replace(collection.counts, media_recovered_count=recovered, media_missing_count=missing,
                         integrity_result='verified')
        bundle = StateBundle(scope, archive_id or uuid4().hex, generation, records, tuple(asdict(counts).items()))
        records = bundle.to_core_records()  # Strict codec's defensive JSON-container projection.
        (staging / 'state').mkdir(exist_ok=True)
        (staging / 'state/schema.json').write_bytes(encode_schema(bundle))
        (staging / 'state/messages.jsonl').write_bytes(encode_messages(bundle))
        for path in (staging / 'assets').rglob('*'):
            if path.is_file():
                durable = staging / 'state' / path.relative_to(staging)
                durable.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, durable)
        first = records[0].message.create_time if records else None
        last = records[-1].message.create_time if records else None
        choice = contact_choice or ContactChoice.from_fields(username=conversation, nickname=conversation)
        resolve = sender_resolver(choice)
        with MarkdownArchiveWriter(staging / 'chat.md', choice.display_name, now, first, last, avatars=avatars) as writer:
            stats = ExportStats()
            for record in records:
                result = MediaResult(record.media_status, f'assets/{record.media_kind}/{record.media_asset}' if record.media_asset else None)
                writer.write_message(record.message, resolve(record.message), result, stats)
        coverage = dict.fromkeys(('fully_supported', 'partial', 'placeholder'), 0)
        for record in records:
            coverage[coverage_bucket(record.message.message_type, record.message.payload)] += 1
        operation_report = dict(shards=shard_report, source_type_counts=source_types,
            final_type_counts=dict(Counter(record.message.message_type for record in records)),
            media=media_counts, coverage=coverage,
            history_2023_count=sum(datetime.fromtimestamp(record.message.create_time).year == 2023 for record in records),
            input_v2_file_count=len(baseline[0]), input_v2_unchanged=True, verified=True)
        metadata = {'capsule_schema_version': 1, 'integrity_status': 'complete', 'mode': mode,
            'scope': asdict(scope), 'message_count': len(records), 'counts': asdict(counts),
            'first_message_time': datetime.fromtimestamp(first).astimezone().isoformat() if first is not None else None,
            'last_message_time': datetime.fromtimestamp(last).astimezone().isoformat() if last is not None else None,
            'state_hashes': {path.relative_to(staging).as_posix(): sha256_file(path)
                for path in sorted((staging / 'state').rglob('*')) if path.is_file()},
            'source_shards': shards, 'source_manifest_sha256': manifest_digest, 'coverage': coverage,
            'message_coverage': {'fully_supported': stats.fully_supported_counts,
                'partial': stats.partial_support_counts, 'placeholder': stats.placeholder_counts},
            'input_v2_hashes': {name: fact[3] for name, fact in baseline[0].items()},
            'coverage_status': 'partial_or_placeholder' if coverage['partial'] or coverage['placeholder'] else 'fully_supported',
            'operation_report': operation_report}
        (staging / 'capsule_meta.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf8')
        if finish_staging is not None:
            finish_staging(staging, bundle, collection.inventory.next_cursors, now)
        generate_current_manifest(staging, created_at=now)
        _validate_output(staging, scope, records)
        if baseline != input_facts():
            raise ValueError('INPUT_CHANGED')
        for path, expected in selected_media_facts.items():
            require_plain_path(path)
            info = path.stat()
            if (info.st_dev, info.st_ino, info.st_size, sha256_file(path)) != expected:
                raise ValueError('INPUT_CHANGED')
        validate_roots()
        if publish is None:
            staging.rename(output)
        else:
            publish(staging, now, records)
        # A successful rename alone is not the published artifact readback.
        # On failure retain this owned output for local diagnosis; never return
        # success or remove/modify any original input.
        _validate_output(output, scope, records)
        operation_report = {**operation_report, 'published_output_verified': True}
    return {'status': 'REAL_BRIDGE_READY' if mode == 'real' else 'SYNTHETIC_READY', 'message_count': len(records),
        'counts': asdict(counts), 'output': str(output), 'real_provider_available': mode == 'real',
        'operation_report': operation_report}
