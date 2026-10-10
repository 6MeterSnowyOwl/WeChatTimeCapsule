"""Historical Core adapter used by the original ExporterService save methods."""
from dataclasses import replace
from datetime import datetime
import hashlib
import json

from .archive import AssetCounts, IncrementalResult
from .contacts import contact_by_username
from .current_capsule import CapsuleResult
from .history_contracts import ArchiveScope
from .history_provider import AuthenticatedHistoryDB, capability_descriptor, discover_sources
from .models import ContactChoice
from .one_shot_provider import select_real_provider
from .product_history import save_product, read_product, validate_target
from .message_identity import normalize_server_id
from .reconciliation import ReconciliationResult, ReconciliationConflict, CONFLICT_FIELDS, _report_path, _report_payload
from .utils import write_json_atomic


def _reconciliation_result(collection, records):
    """Project actual Core observations into the original report contract.

    Database/matched/conflict counts are distinct canonical messages. Core raw
    representation counts remain separate in the report, never guessed by
    subtracting legacy identity-migration counts.
    """
    retained = {record.identity: record for record in collection.migration.records}
    observations = {}
    for row in collection.inventory.rows:
        identity = normalize_server_id(row.server_id, row.locator.storage_classes[0])
        observations.setdefault(identity, []).append(row.message)
    conflicts, matched = [], 0
    for identity, messages in observations.items():
        if identity not in retained:
            continue
        reference = retained[identity].message
        fields = tuple(field for field in CONFLICT_FIELDS
            if any(getattr(reference, field) != getattr(message, field) for message in messages))
        if fields:
            conflicts.append(ReconciliationConflict(identity, fields, 'sort_seq' in fields))
        else:
            matched += 1
    incremental = IncrementalResult(records, collection.new_identities,
        collection.counts.canonical_duplicate_count, 'reconciliation', collection.counts.raw_selected_count)
    return ReconciliationResult(incremental, len(observations), collection.counts.archive_before_count,
        matched, len(collection.new_identities), tuple(conflicts))


def authenticated_binding(service, conversation):
    db, backend = service._db()  # Actual authentication cache, never READY's probe DB.
    selected = next((a for a in service.account_options()
                     if a.stable_id == service._selected_account_identity), None)
    if (selected is None or backend.package_version != '1.2.1'
            or backend.wxid != selected.stable_id or backend.account != selected.account_locator
            or vars(db).get('account') != selected.account_locator):
        raise ValueError('PRODUCT_RUNTIME_ACCOUNT_MISMATCH')
    binding = AuthenticatedHistoryDB(db, selected.account_locator)
    capability = capability_descriptor(binding)
    media = capability.storage_root.parent
    descriptor, media_db, domains = select_real_provider(authenticated_db=binding,
        account=selected.account_locator, conversation=conversation, source_root=capability.storage_root,
        media_root=media, expected_shards=tuple(s.relative_db for s in discover_sources(capability.storage_root)),
        metadata_paths=capability.metadata,
        image_domain=media / 'msg/attach' / hashlib.md5(conversation.encode()).hexdigest(),
        video_domain=media / 'msg/video', origin_confirmed=True, real_read_confirmed=True)
    descriptor = replace(descriptor, capability=replace(descriptor.capability, account_id=selected.stable_id))
    return db, descriptor, media_db, domains, ArchiveScope(selected.stable_id, conversation)


def save_history(service, target_root, mode):
    from .service import BackupOutcome
    service._ensure_backup_compatible()
    target = service.find_target(target_root)
    validate_target(target.root, ArchiveScope(service._selected_account_identity, target.username))
    db, descriptor, media_db, domains, scope = authenticated_binding(service, target.username)
    contact = contact_by_username(db, target.username) or ContactChoice.from_fields(
        username=target.username, nickname=target.display_name)
    started_at = datetime.now().astimezone()
    collected = []
    def capture_avatars(staging):
        from .avatars import AvatarExporter
        exporter = AvatarExporter(db, staging / 'assets/avatars', service.logger)
        return {role: exporter.export(role, username, label,
                    input_func=lambda _: '', print_func=lambda *args: None)
                for role, username, label in (('self', scope.account_id, '我的'),
                    ('contact', target.username, '对方'))}
    result = save_product(descriptor=descriptor, media_db=media_db, domains=domains,
        scope=scope, contact=contact, output=target.root, work_root=service.runtime_work_root(), mode=mode,
        collection_ready=collected.append if mode == 'full' else None, capture_avatars=capture_avatars)
    bundle, _ = read_product(target.root, scope)
    metadata = json.loads((target.root / 'capsule_meta.json').read_text(encoding='utf8'))
    records = bundle.to_core_records()
    media = result['operation_report']['media']
    capsule = CapsuleResult(target.root, metadata['last_saved_at'],
        'reconciliation' if metadata['save_mode'] == 'full' else 'quick', len(records),
        result['counts']['newly_discovered_count'],
        sum(r.media_kind == 'images' and bool(r.media_asset) for r in records),
        media['images']['recovered'] - media['images']['reused'],
        sum(r.media_kind == 'videos' and bool(r.media_asset) for r in records),
        media['videos']['recovered'] - media['videos']['reused'],
        AssetCounts(reused=sum(x['reused'] for x in media.values()),
                    new=sum(x['recovered'] - x['reused'] for x in media.values())),
        coverage_summary=metadata.get('message_coverage', {}),
        avatar_history_count=metadata.get('avatar_history_count', 0))
    if mode != 'full':
        return BackupOutcome(capsule)
    reconciliation = _reconciliation_result(collected[0], records)
    completed_at = datetime.now().astimezone()
    report_path = _report_path(target, completed_at)
    payload = _report_payload(target, reconciliation, started_at, completed_at, None)
    payload['core_counts'] = result['counts']
    payload['current_capsule_updated'] = True
    payload['count_semantics'] = 'database/matched/conflicts: distinct canonical messages; core_counts: source representations'
    write_json_atomic(report_path, payload)
    return BackupOutcome(capsule, reconciliation, report_path)
