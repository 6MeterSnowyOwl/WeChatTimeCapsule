"""Disposable synthetic child; frozen algorithms are imported unchanged."""
from pathlib import Path
import hashlib
import json
import os
import sys
import tempfile
import time


def isolated_imports(project):
    sys.path[:0] = [str(project / ".venv-release-py312" / "Lib" / "site-packages"), str(project)]


def hold_lock(project, namespace, proof_bytes):
    isolated_imports(project)
    from wechat_exporter.track2_contracts import LegacyNamespaceProof, decode_dto
    from wechat_exporter.v3_profile import open_v3_profile
    from wechat_exporter.namespace_lock import authority_lock
    profile = open_v3_profile(namespace, "SYNTHETIC", decode_dto(LegacyNamespaceProof, proof_bytes))
    with authority_lock(profile, "child-holder", timeout_seconds=2):
        print(json.dumps({"lease_acquired": True, "pid": os.getpid()}), flush=True)
        # Parent always reaps this disposable child; bounded escape if it dies.
        time.sleep(25)


def frozen_bootstrap(project):
    pinned = project / ".venv-release-py312" / "Lib" / "site-packages"
    frozen = project / "tests" / "fixtures" / "legacy_rc6"
    sys.path[:0] = [str(frozen), str(pinned)]
    sources = sorted((frozen / "wechat_exporter").glob("*.py"))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest = json.loads((frozen / "SOURCE_HASHES.json").read_bytes())
    verified = {relative: hashlib.sha256((frozen / relative).read_bytes()).hexdigest()
                for relative in manifest["files"]}
    assert verified == manifest["files"], "immutable frozen source manifest changed"
    with tempfile.TemporaryDirectory(prefix="synthetic-track2-frozen-bootstrap-") as tmp:
        base = Path(tmp)
        documents, local, old, exports, known, registered, candidate = [base / name for name in
            ("documents", "local", "old", "exports", "known", "registered", "v3-candidate")]
        for root in (documents, local, old, exports, known, registered, candidate):
            root.mkdir()
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["LOCALAPPDATA"] = str(local)
        # Build the genuine new namespace before importing frozen startup. The
        # default/fallback old locations are conservatively covered by local.
        legacy_appdata = local / "WeChatTimeCapsule"
        legacy_appdata.mkdir()
        settings_path = legacy_appdata / "settings.json"
        registry_path = legacy_appdata / "archive_registry.json"
        def write_json(path, value):
            path.write_text(json.dumps(value), encoding="utf8")
        def target(root, identity):
            root.mkdir(exist_ok=True)
            write_json(root / "target.json", {"schema_version": 2,
                "username": identity, "display_name": "synthetic"})
        target(exports / "exported", "synthetic-exported")
        target(known / "known", "synthetic-known")
        target(registered, "synthetic-registered")
        # Recognizable to the actual old detector if accidentally scanned.
        target(candidate / "v3-peer", "synthetic-v3")
        write_json(settings_path, {"schema_version": 2,
            "archive_root": str(documents / "微信时间胶囊"), "known_archive_roots": [str(known)],
            "onboarding_completed": False, "window_size": [900, 650], "last_selected_target": "",
            "selected_account_identity": "", "suppressed_first_backup_notices": []})
        write_json(registry_path, {"schema_version": 1, "archives": [{
            "archive_id": "synthetic-registered-id", "path": str(registered)}],
            "bootstrap_completed": False})
        isolated_imports(project)
        from wechat_exporter.physical_paths import prove_existing_root, snapshot_tree
        from wechat_exporter.track2_contracts import LegacyNamespaceProof, CandidateRecord
        from wechat_exporter.history_contracts import ArchiveScope
        from wechat_exporter.v3_profile import open_v3_profile
        from wechat_exporter.namespace_lock import authority_lock
        from wechat_exporter.v3_registry import register_candidate, read_binding
        domains = (documents, local, old, exports, known, registered)
        legacy_proof = LegacyNamespaceProof(domains, (),
            hashlib.sha256(settings_path.read_bytes()).hexdigest(),
            hashlib.sha256(registry_path.read_bytes()).hexdigest(),
            tuple((root, prove_existing_root(root).identity) for root in domains))
        old_before = tuple(snapshot_tree(root) for root in domains)
        profile = open_v3_profile(base / "WeChatTimeCapsule-v3", "SYNTHETIC", legacy_proof)
        record = CandidateRecord("candidate-1", "object-1", ArchiveScope("synthetic-account", "synthetic-v3"),
            "SYNTHETIC", prove_existing_root(candidate).identity, "BUILDING", None, None, "synthetic-build")
        with authority_lock(profile, "synthetic-index") as lease:
            register_candidate(profile, record, candidate, lease)
        assert read_binding(profile, "object-1") is None
        assert old_before == tuple(snapshot_tree(root) for root in domains)
        for name in tuple(sys.modules):
            if name == "wechat_exporter" or name.startswith("wechat_exporter."):
                del sys.modules[name]
        sys.path.remove(str(project))
        sys.path.insert(0, str(frozen))
        from unittest.mock import patch
        from PySide6.QtCore import QStandardPaths
        with patch.object(QStandardPaths, "writableLocation", return_value=str(documents)):
            from wechat_exporter.service import AppSettings, ExporterService
            from wechat_exporter.registry import ArchiveRegistry
            from wechat_exporter.archive import discover_targets
            assert Path(sys.modules["wechat_exporter.service"].__file__).parent.parent == frozen
            settings = AppSettings.default(base)
            assert settings.path == settings_path and settings.project_exports == exports
            registry = ArchiveRegistry(registry_path)
            assert registry.bootstrap_completed is False
            trace = []
            auditing = [True]
            instrument = {"open", "os.listdir", "os.scandir", "os.rename", "os.remove",
                          "os.rmdir", "os.mkdir"}
            def audit(event, args):
                if not auditing[0] or event not in instrument:
                    return
                for item in args[:2]:
                    if isinstance(item, (str, bytes, os.PathLike)):
                        path = Path(os.fsdecode(item))
                        if path.is_absolute():
                            try:
                                relative = path.relative_to(base)
                            except ValueError:
                                raise AssertionError("FROZEN_RUNTIME_ESCAPE") from None
                            trace.append({"event": event, "relative": relative.as_posix()})
                            if (path == candidate or candidate in path.parents
                                or path == profile.namespace_root or profile.namespace_root in path.parents):
                                raise AssertionError("FROZEN_V3_ACCESS")
            sys.addaudithook(audit)
            def forbidden(*args, **kwargs):
                raise AssertionError("REAL_PROVIDER_CALLED")
            settings_before = settings_path.read_bytes()
            real_stat, real_lstat = os.stat, os.lstat
            def stat(path, *args, **kwargs):
                audit("open", (path,))
                return real_stat(path, *args, **kwargs)
            def lstat(path, *args, **kwargs):
                audit("open", (path,))
                return real_lstat(path, *args, **kwargs)
            try:
                with patch.object(os, "stat", side_effect=stat), patch.object(os, "lstat", side_effect=lstat):
                    service = ExporterService(base, settings=settings, registry=registry,
                        account_lister=forbidden, database_factory=forbidden,
                        compatibility_provider=forbidden, probe_runner=forbidden)
                    expected = {"synthetic-registered", "synthetic-known", "synthetic-exported"}
                    assert {t.username for t in service._targets()} == expected
                    assert {t.username for t in discover_targets(exports)} == {"synthetic-exported"}
                    assert registry.bootstrap_completed is True
                    assert settings_path.read_bytes() == settings_before
                    assert any(t["event"] == "os.rename" for t in trace), "real old registry commit not observed"
                    assert all(not t["relative"].startswith("v3-candidate") for t in trace)
                    # Logging owns an old-namespace handle; close before temp cleanup.
                    for handler in tuple(service.logger.handlers):
                        handler.close()
                        service.logger.removeHandler(handler)
                    print(json.dumps({"bootstrap": True, "legacy_targets": 3,
                        "new_profile_initialized": True, "candidate_registered": True,
                        "new_profile_oldbytes_preserved": True, "v3_accesses": 0,
                        "provider_calls": 0, "trace": trace,
                        "frozen_manifest_verified": True, "frozen_manifest_file_count": len(verified),
                        "frozen_source_digests": before}, sort_keys=True), flush=True)
            finally:
                # The boundary covers frozen runtime, never the test's teardown.
                auditing[0] = False
                import logging
                logging.shutdown()


if __name__ == "__main__":
    mode, project = sys.argv[1], Path(sys.argv[2])
    if mode == "lock":
        hold_lock(project, Path(sys.argv[3]), sys.argv[4].encode("utf8"))
    elif mode == "bootstrap":
        frozen_bootstrap(project)
    else:
        raise AssertionError("UNAPPROVED_SYNTHETIC_CHILD_MODE")
