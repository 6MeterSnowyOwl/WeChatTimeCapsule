"""Synthetic source provenance, native local media, and archived asset contracts."""
from dataclasses import replace
import hashlib
import logging
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from history_fixtures import identity_inventory, identity_row, synthetic_media_history
from wechat_exporter.archive import ArchivedMessage
from wechat_exporter.history_collection import collect_history
from wechat_exporter.history_contracts import HistoryError, ScopeAuthorization, SourceCount
from wechat_exporter.identity_migration import plan_identity_migration
from wechat_exporter.media import ImageExporter, VideoExporter
from wechat_exporter.message_identity import resolve_canonical_rows
try:
    from wechat_exporter import source_media
except ImportError:
    source_media = None


LOGGER = logging.getLogger("synthetic-historical-media")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


class MediaAbsenceSession:
    """Borrowed all-memory session with observable no-I/O sentinels."""
    def __init__(self, inventory):
        self.inventory = inventory
        self.raw_reads = 0
        self.stability_checks = 0
        self.closed = False

    def assert_source_set_stable(self):
        self.stability_checks += 1

    def resolve_raw_row(self, token):
        self.raw_reads += 1
        raise AssertionError("UNEXPECTED_RAW_LOOKUP")


class UnusedMediaRoot:
    def __init__(self):
        self.accesses = 0

    def __fspath__(self):
        self.accesses += 1
        raise AssertionError("UNEXPECTED_STAGING_ACCESS")


class HistoricalMediaTests(unittest.TestCase):
    def absent_collection(self, mode="full", *, relocated=False):
        row = identity_row(raw_type=3, message_type="图片")
        prior_inventory = identity_inventory(row)
        old = resolve_canonical_rows(prior_inventory, ()).records[0]
        if relocated:
            moved = identity_row(raw_type=3, message_type="图片", local="2")
            old = resolve_canonical_rows(identity_inventory(moved), (old,)).records[0]
        count = SourceCount(row.locator.source_key, 1 if mode == "quick" else 0, 0, 0, 0)
        inventory = replace(prior_inventory, mode=mode, rows=(), counts=(count,))
        session = MediaAbsenceSession(inventory)
        collection = collect_history((old,), session, mode=mode,
                                     authorization=ScopeAuthorization(inventory.scope, True))
        return old, session, collection

    def check_absent_collection_media(self, mode, *, relocated=False):
        old, session, collection = self.absent_collection(mode, relocated=relocated)
        root = UnusedMediaRoot()
        with patch("wechat_exporter.media.ImageExporter.export", side_effect=AssertionError("UNEXPECTED_DECODE")) as decode:
            result = self.module().export_record_media(session, collection.records[0], object(), root, logger=LOGGER)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "LOCAL_MEDIA_UNAVAILABLE")
        self.assertFalse(result.relative_path)
        self.assertIs(collection.records[0].message, old.message)
        self.assertEqual(collection.records[0].source_refs, old.source_refs)
        self.assertEqual(collection.records[0].message.local_id, "1")
        self.assertEqual((collection.counts.newly_discovered_count, collection.counts.final_union_count), (0, 1))
        self.assertEqual((session.raw_reads, root.accesses, decode.call_count), (0, 0, 0))
        self.assertFalse(session.closed)
        self.assertEqual(session.stability_checks, 1)
        return collection

    def test_collection_retained_historical_ref_without_current_source_is_placeholder(self):
        collection = self.check_absent_collection_media("full")
        self.assertEqual(collection.counts.count_scope, "full")
        self.assertEqual(collection.counts.raw_source_count, 0)

    def test_quick_window_absence_does_not_claim_global_source_absence(self):
        collection = self.check_absent_collection_media("quick")
        self.assertEqual(collection.counts.count_scope, "quick_window")
        self.assertEqual((collection.counts.raw_source_count, collection.counts.raw_selected_count), (1, 0))
        self.assertIsNone(collection.counts.archive_only_count)

    def test_previously_relocated_historical_ref_can_be_absent(self):
        collection = self.check_absent_collection_media("full", relocated=True)
        self.assertEqual(collection.records[0].source_refs[0].local_id, "2")

    def test_absence_does_not_require_new_technical_observation_evidence(self):
        old, session, _ = self.absent_collection()
        old = replace(old, technical_observations=())
        collection = collect_history((old,), session, mode="full",
                                     authorization=ScopeAuthorization(session.inventory.scope, True))
        root = UnusedMediaRoot()
        result = self.module().export_record_media(session, collection.records[0], object(), root, logger=LOGGER)
        self.assertEqual(result.reason, "LOCAL_MEDIA_UNAVAILABLE")
        self.assertEqual((session.raw_reads, root.accesses), (0, 0))
        self.assertEqual(collection.records[0].technical_observations, ())

    def test_absence_does_not_hide_invalid_historical_metadata(self):
        old, session, _ = self.absent_collection()
        ref = old.source_refs[0]
        wrong_scope = replace(old.scope, conversation_id="synthetic-other")
        invalid_records = (
            replace(old, canonical_identity="server:0"),
            replace(old, canonical_identity="server:0101"),
            replace(old, binding_status="archive_only"),
            replace(old, scope=wrong_scope),
            replace(old, source_refs=(replace(ref, scope=wrong_scope),)),
            replace(old, source_refs=(replace(ref, expected_server_id="999"),)),
            replace(old, source_refs=(replace(ref, local_id="invalid"),)),
            replace(old, source_refs=(replace(ref, local_id="01"),)),
            replace(old, source_refs=(replace(ref, create_time=1700000001),)),
            replace(old, source_refs=(replace(ref, sort_seq=11),)),
            replace(old, source_refs=(replace(ref, storage_classes=("integer", "text", "integer", "integer", "integer")),)),
            replace(old, source_refs=(replace(ref, raw_local_type=43),)),
            replace(old, technical_observations=({"raw_local_type": "invalid", "storage_classes": ("integer",) * 5},)),
            replace(old, source_refs=(), canonical_identity=None),
        )
        for index, record in enumerate(invalid_records):
            with self.subTest(case=index):
                root = UnusedMediaRoot()
                with self.assertRaises(HistoryError) as raised:
                    self.module().export_record_media(session, record, object(), root, logger=LOGGER)
                self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_MISMATCH")
                self.assertEqual((session.raw_reads, root.accesses), (0, 0))

    def test_current_candidate_or_occupied_ref_keeps_stale_ref_fatal(self):
        old, _, _ = self.absent_collection()
        candidates = (
            identity_row(raw_type=3, message_type="图片", local="2", source=1),
            identity_row(raw_type=3, message_type="图片", server="999"),
            identity_row(raw_type=3, message_type="图片", create_time=1700000001),
        )
        for index, row in enumerate(candidates):
            with self.subTest(case=index):
                session, root = MediaAbsenceSession(identity_inventory(row)), UnusedMediaRoot()
                with self.assertRaises(HistoryError) as raised:
                    self.module().export_record_media(session, old, object(), root, logger=LOGGER)
                self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_MISMATCH")
                self.assertEqual((session.raw_reads, root.accesses), (0, 0))

    def test_source_absence_still_validates_existing_referenced_asset(self):
        old, session, _ = self.absent_collection()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record, manifest, _ = self.asset(root, old)
            (root / "state/assets/images/unchanged.png").unlink()
            staging = UnusedMediaRoot()
            with self.assertRaises(HistoryError) as raised:
                self.module().export_record_media(session, record, object(), staging,
                                                 archived_root=root, manifest=manifest, logger=LOGGER)
            self.assertEqual(raised.exception.code, "ARCHIVED_ASSET_INTEGRITY_FAILED")
            self.assertEqual((session.raw_reads, staging.accesses), (0, 0))

    def module(self):
        self.assertIsNotNone(source_media, "source-qualified media behavior is missing")
        return source_media

    def records(self, fixture):
        records = resolve_canonical_rows(fixture.session.inventory, ()).records
        # Reader's test decoder models message semantics, not native media decoding.
        return tuple(replace(record, message=replace(record.message,
                     message_type="图片" if index < 2 else "视频"))
                     for index, record in enumerate(records))

    def export(self, fixture, record, **kwargs):
        return self.module().export_record_media(fixture.session, record, fixture.db,
                 fixture.output, logger=LOGGER, **kwargs)

    def asset(self, root, record):
        payload = b"synthetic-old-asset"
        relative = "assets/images/unchanged.png"
        for path in (root / relative, root / "state" / relative):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        manifest = {"schema_version": 1, "capsule": "current", "files": {
            relative: {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}}}
        return replace(record, media_status="success", media_kind="images",
                       media_asset="unchanged.png"), manifest, payload

    def test_downloader_history_error_is_operation_fatal(self):
        # Removing explicit HistoryError propagation makes this fail on legacy too.
        from history_fixtures import identity_row
        message = identity_row().message
        for exporter_type, method in ((ImageExporter, "download_image"),
                                      (VideoExporter, "download_video")):
            with self.subTest(kind=exporter_type.__name__), TemporaryDirectory() as directory:
                exporter = exporter_type(object(), "synthetic", Path(directory), LOGGER)
                with patch.object(exporter.downloader, method,
                                  side_effect=HistoryError("SOURCE_IDENTITY_MISMATCH")):
                    with self.assertRaises(HistoryError) as raised:
                        exporter.export(message)
                self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_MISMATCH")

    def test_colliding_local_time_image_video_sources(self):
        from wechatauto.media import MediaDownloader
        with synthetic_media_history() as f:
            native_paths = []
            native_out = MediaDownloader._out
            def observe(downloader, save_dir, name):
                result = native_out(downloader, save_dir, name)
                native_paths.append(Path(result))
                return result
            with patch.object(MediaDownloader, "_out", observe):
                results = [self.export(f, record) for record in self.records(f)]
            self.assertEqual([result.status for result in results], ["success"] * 4)
            self.assertEqual([(f.output / result.relative_path).read_bytes() for result in results], list(f.expected))
            self.assertEqual(len({result.relative_path for result in results}), 4)
            self.assertEqual(len({path.parent for path in native_paths}), 4)
            self.assertEqual(f.db.shared_calls, 0)

    def test_stale_locator_identity_error_is_not_placeholder(self):
        self.module()
        with synthetic_media_history() as f:
            record = self.records(f)[0]
            stale = replace(record.source_refs[0], expected_server_id="999")
            for changed in (replace(record, source_refs=(stale,)),
                            replace(record, scope=replace(f.scope, conversation_id="wrong")),
                            replace(record, source_refs=(replace(record.source_refs[0], local_id="9"),))):
                with self.subTest(), self.assertRaises(HistoryError) as raised:
                    self.export(f, changed)
                self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_MISMATCH")
            self.assertFalse(f.output.exists())
            self.assertEqual(f.db.shared_calls, 0)

    def test_relocated_valid_snapshot_ref(self):
        with synthetic_media_history(relocated=True) as f:
            current = self.records(f)[0]
            archived_message = replace(current.message, local_id="1", content="synthetic archived authority")
            old = replace(current, message=archived_message,
                          source_refs=(replace(current.source_refs[0], local_id="1"),))
            record = resolve_canonical_rows(f.session.inventory, (old,)).records[0]
            result = self.export(f, record)
            self.assertEqual(result.status, "success")
            self.assertEqual((f.output / result.relative_path).read_bytes(), f.expected[0])
            self.assertIs(record.message, archived_message)
            self.assertEqual(record.message.local_id, "1")
            self.assertEqual(record.source_refs[0].local_id, "2")
            self.assertEqual(f.db.shared_calls, 0)

    def test_retained_raw_identity_and_storage_are_revalidated(self):
        with synthetic_media_history() as f:
            record = self.records(f)[0]
            raw = f.session.resolve_raw_row(f.session.inventory.rows[0].raw_row_token)
            for field, value in (("server_id", 999), ("server_id", "101"),
                                 ("local_id", 2), ("create_time", 1700000001),
                                 ("sort_seq", 11), ("local_type", 43)):
                with self.subTest(field=field, value_type=type(value).__name__):
                    with patch.object(f.session, "resolve_raw_row", return_value={**raw, field: value}):
                        with self.assertRaises(HistoryError) as raised:
                            self.export(f, record)
                    self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_MISMATCH")
            with patch.object(f.session, "resolve_raw_row", side_effect=HistoryError("SOURCE_READ_FAILED")):
                with self.assertRaises(HistoryError) as raised:
                    self.export(f, record)
            self.assertEqual(raised.exception.code, "SOURCE_READ_FAILED")
            self.assertFalse(f.output.exists())
            self.assertEqual(f.db.shared_calls, 0)

    def test_facade_allowlist_and_native_blob_mapping(self):
        with synthetic_media_history() as f:
            validated = self.module().resolve_media_row(f.session, self.records(f)[0])
            facade = source_media.BoundMediaDB(f.db, validated)
            raw = facade.get_message_row(f.scope.conversation_id, 1)
            retained = f.session.resolve_raw_row(f.session.inventory.rows[0].raw_row_token)
            self.assertEqual(raw["content"], retained["message_content"])
            self.assertEqual(raw["packed_info"], retained["packed_info_data"])
            self.assertIsInstance(raw["content"], bytes)
            self.assertEqual(set(raw), {"local_id", "local_type", "server_id", "sender_id",
                "sender_username", "create_time", "content", "source", "packed_info", "compress_content", "sort_seq"})
            for name in ("_open", "_db_files", "get_nickname", "_sender_id_index", "_find_weixin_pids", "secret"):
                with self.assertRaises(AttributeError):
                    getattr(facade, name)
            for user, local in (("wrong", 1), (f.scope.conversation_id, 2)):
                with self.assertRaises(HistoryError) as raised:
                    facade.get_message_row(user, local)
                self.assertEqual(raised.exception.code, "SOURCE_IDENTITY_MISMATCH")
            raw["content"] = b"mutated copy"
            self.assertEqual(facade.get_message_row(f.scope.conversation_id, 1)["content"], retained["message_content"])
            self.assertEqual(f.db.shared_calls, 0)

    def test_missing_new_historical_media_placeholder(self):
        with synthetic_media_history(missing=True) as f:
            records = self.records(f)
            results = [self.export(f, record) for record in records]
            self.assertEqual([result.status for result in results], ["failed"] * 4)
            self.assertTrue(all(not result.relative_path for result in results))
            self.assertEqual(len(records), 4)
            self.assertEqual(f.db.shared_calls, 0)

    def test_identity_rekey_reuses_media_without_decode(self):
        with synthetic_media_history() as f:
            record, manifest, payload = self.asset(f.root / "archive", self.records(f)[0])
            legacy = replace(record, canonical_identity=None, scope=None, binding_status="",
                             source_refs=(), technical_observations=())
            plan = plan_identity_migration((legacy,), identity_inventory(f.session.inventory.rows[0]),
                                           ScopeAuthorization(f.scope, True))
            self.assertEqual(plan.newly_discovered_count, 0)
            self.assertIs(plan.records[0].message, legacy.message)
            archive_only = plan_identity_migration((legacy,), identity_inventory(),
                                                   ScopeAuthorization(f.scope, True)).records[0]
            for record in (plan.records[0], archive_only):
                with patch("wechat_exporter.media.ImageExporter.export", side_effect=AssertionError("DECODE_OLD_ASSET")) as decode:
                    result = self.export(f, record, archived_root=f.root / "archive", manifest=manifest)
                self.assertEqual(decode.call_count, 0)
                self.assertEqual(result.relative_path, "assets/images/unchanged.png")
                self.assertEqual((f.root / "archive" / result.relative_path).read_bytes(), payload)
                self.assertEqual(record.media_asset, "unchanged.png")
            self.assertFalse(f.output.exists())
            self.assertEqual(f.db.shared_calls, 0)

    def test_native_failure_reason_is_sanitized_for_canonical_export(self):
        from history_fixtures import identity_row
        for exporter_type, method in ((ImageExporter, "download_image"),
                                      (VideoExporter, "download_video")):
            with self.subTest(kind=exporter_type.__name__), TemporaryDirectory() as directory:
                exporter = exporter_type(object(), "synthetic", Path(directory), LOGGER)
                with patch.object(exporter.downloader, method,
                                  side_effect=OSError("SENSITIVE_SYNTHETIC_PATH_AND_BODY")):
                    result = exporter.export(identity_row().message, canonical_identity="server:101")
                self.assertEqual(result.reason, "LOCAL_MEDIA_UNAVAILABLE")
                self.assertEqual(result.status, "failed")

    def assert_materialization_failure(self, action, marker):
        with self.assertNoLogs(LOGGER, level="WARNING"):
            try:
                action()
            except Exception as error:
                self.assertIs(type(error), HistoryError)
                self.assertEqual(error.code, "SOURCE_READ_FAILED")
                self.assertEqual(str(error), "SOURCE_READ_FAILED")
                self.assertNotIn(marker, repr(error.summary))
                self.assertNotIn(marker, "".join(traceback.format_exception(error)))
                self.assertIsNone(error.__cause__)
                self.assertTrue(error.__suppress_context__)
            else:
                self.fail("materialization failure was not operation-fatal")

    def test_canonical_final_open_and_copy_failures_are_sanitized_fatal(self):
        with synthetic_media_history() as f:
            for index, exporter_type, kind in ((0, ImageExporter, "images"), (2, VideoExporter, "videos")):
                record = self.records(f)[index]
                facade = self.module().BoundMediaDB(f.db, source_media.resolve_media_row(f.session, record))
                exporter = exporter_type(facade, f.scope.conversation_id, f.output / "assets" / kind, LOGGER)
                for phase in ("exclusive_open", "copy"):
                    with self.subTest(kind=kind, phase=phase):
                        marker = "SYNTHETIC_PRIVATE_MATERIALIZATION_MARKER"
                        original_open = Path.open
                        def fail_exclusive_open(path, mode="r", *args, **kwargs):
                            if mode == "xb":
                                raise OSError(marker)
                            return original_open(path, mode, *args, **kwargs)
                        boundary = (patch.object(Path, "open", fail_exclusive_open) if phase == "exclusive_open"
                                    else patch("wechat_exporter.media.shutil.copyfileobj", side_effect=OSError(marker)))
                        with boundary:
                            self.assert_materialization_failure(
                                lambda: exporter.export(record.message, canonical_identity=record.canonical_identity), marker)
            self.assertEqual(f.db.shared_calls, 0)

    def test_canonical_temporary_preparation_failures_are_sanitized_fatal(self):
        from history_fixtures import identity_row
        for exporter_type in (ImageExporter, VideoExporter):
            with self.subTest(kind=exporter_type.__name__), TemporaryDirectory() as directory:
                exporter = exporter_type(object(), "synthetic", Path(directory), LOGGER)
                marker = "SYNTHETIC_PRIVATE_PREPARATION_MARKER"
                with patch("wechat_exporter.media.TemporaryDirectory", side_effect=OSError(marker)):
                    self.assert_materialization_failure(
                        lambda: exporter.export(identity_row().message, canonical_identity="server:101"), marker)

    def test_helper_preparation_failures_are_sanitized_fatal(self):
        with synthetic_media_history() as f:
            for phase in ("root_mkdir", "temporary", "exporter_mkdir"):
                for index in (0, 2):
                    with self.subTest(phase=phase, media_index=index):
                        record = self.records(f)[index]
                        marker = "SYNTHETIC_PRIVATE_HELPER_MARKER"
                        original_mkdir = Path.mkdir
                        target = f.output if phase == "root_mkdir" else f.output / "assets" / ("images" if index == 0 else "videos")
                        def fail_target_mkdir(path, *args, **kwargs):
                            if path == target:
                                raise OSError(marker)
                            return original_mkdir(path, *args, **kwargs)
                        boundary = (patch("wechat_exporter.source_media.TemporaryDirectory", side_effect=OSError(marker))
                                    if phase == "temporary" else patch.object(Path, "mkdir", fail_target_mkdir))
                        with boundary:
                            self.assert_materialization_failure(lambda: self.export(f, record), marker)
            self.assertEqual(f.db.shared_calls, 0)

    def test_materialization_boundaries_preserve_existing_history_error(self):
        with synthetic_media_history() as f:
            failure = HistoryError("SOURCE_READ_TIMEOUT")
            with patch.object(Path, "mkdir", side_effect=failure):
                with self.assertRaises(HistoryError) as raised:
                    self.export(f, self.records(f)[0])
            self.assertIs(raised.exception, failure)
            for index, exporter_type, kind in ((0, ImageExporter, "images"), (2, VideoExporter, "videos")):
                record = self.records(f)[index]
                facade = self.module().BoundMediaDB(f.db, source_media.resolve_media_row(f.session, record))
                exporter = exporter_type(facade, f.scope.conversation_id, f.output / "assets" / kind, LOGGER)
                with patch("wechat_exporter.media.shutil.copyfileobj", side_effect=failure):
                    with self.assertRaises(HistoryError) as raised:
                        exporter.export(record.message, canonical_identity=record.canonical_identity)
                self.assertIs(raised.exception, failure)

    def test_legacy_materialization_failure_keeps_default_exception_behavior(self):
        with synthetic_media_history() as f:
            failure = OSError("SYNTHETIC_LEGACY_FAILURE")
            for index, exporter_type, kind in ((0, ImageExporter, "images"), (2, VideoExporter, "videos")):
                record = self.records(f)[index]
                facade = self.module().BoundMediaDB(f.db, source_media.resolve_media_row(f.session, record))
                exporter = exporter_type(facade, f.scope.conversation_id, f.output / "assets" / kind, LOGGER)
                with patch.object(Path, "replace", side_effect=failure):
                    with self.assertRaises(OSError) as raised:
                        exporter.export(record.message)
                self.assertIs(raised.exception, failure)

    def test_archived_missing_or_corrupt_asset_blocks_manifest_regeneration(self):
        self.module()
        with synthetic_media_history() as f:
            root = f.root / "archive"
            for relative in ("assets/images/unchanged.png", "state/assets/images/unchanged.png"):
                for action in ("missing", "corrupt"):
                    with self.subTest(relative=relative, action=action):
                        record, manifest, _ = self.asset(root, self.records(f)[0])
                        target = root / relative
                        if action == "missing":
                            target.unlink()
                        else:
                            target.write_bytes(b"synthetic-corrupted")
                        before = repr(manifest)
                        with self.assertRaises(HistoryError) as raised:
                            self.export(f, record, archived_root=root, manifest=manifest)
                        self.assertEqual(raised.exception.code, "ARCHIVED_ASSET_INTEGRITY_FAILED")
                        self.assertEqual(repr(manifest), before)
            self.assertFalse(f.output.exists())

    def test_manifest_cannot_omit_reference_or_bless_matching_corrupt_copies(self):
        self.module()
        with synthetic_media_history() as f:
            root = f.root / "archive"
            record, manifest, _ = self.asset(root, self.records(f)[0])
            with self.assertRaises(HistoryError):
                source_media.validate_archived_assets(root, (record,), {"files": {}})
            for path in (root / "assets/images/unchanged.png", root / "state/assets/images/unchanged.png"):
                path.write_bytes(b"same-but-corrupt")
            with self.assertRaises(HistoryError):
                source_media.validate_archived_assets(root, (record,), manifest)

    def test_archive_only_without_asset_is_placeholder_without_lookup(self):
        with synthetic_media_history() as f:
            record = replace(self.records(f)[0], source_refs=(), canonical_identity="archive:bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
                             binding_status="archive_only")
            self.assertEqual(self.export(f, record).status, "failed")
            self.assertFalse(f.output.exists())
            self.assertEqual(f.db.shared_calls, 0)

    def test_repeated_export_never_overwrites_existing_asset(self):
        with synthetic_media_history() as f:
            record = self.records(f)[0]
            first = self.export(f, record)
            path = f.output / first.relative_path
            path.write_bytes(b"preexisting-protected")
            second = self.export(f, record)
            self.assertNotEqual(first.relative_path, second.relative_path)
            self.assertEqual(path.read_bytes(), b"preexisting-protected")
            self.assertEqual((f.output / second.relative_path).read_bytes(), f.expected[0])


if __name__ == "__main__":
    unittest.main()
