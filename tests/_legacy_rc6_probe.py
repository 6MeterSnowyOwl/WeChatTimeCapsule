"""Synthetic subprocess characterization; no production methods are replaced."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile


def tree_hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def make_archive(root):
    """Hand-authored contract fixture, never derived from an existing archive."""
    scope = {"account_id": "synthetic_account", "conversation_id": "synthetic_peer"}
    write_json(root / "target.json", {
        "schema_version": 2, "username": "synthetic_peer", "display_name": "synthetic",
        "identity_contract": {"version": 1, "scope": scope, "origin_confirmed": True},
    })
    write_json(root / "snapshot_index.json", {
        "schema_version": 2, "snapshots": [{"id": "legacy_sample", "message_count": 1}],
    })
    write_json(root / "archive_state.json", {
        "schema_version": 3, "scope": scope, "message_count": 1,
        "last_key": [1700000000, 1, "server:9007199254740993"],
        "per_source_cursors": {}, "identity_contract_version": 1,
    })
    write_json(root / "state" / "schema.json", {
        "schema_version": 3, "format": "normalized-message-jsonl",
        "scope": scope, "identity": "scoped-server-or-archive",
        "sort_keys": ["create_time", "sort_seq", "identity"], "source_locator_version": 1,
    })
    write_json(root / "state" / "messages.jsonl", {
        "identity": "server:9007199254740993", "scope": scope,
        "local_id": "1", "message_type": "图片", "sender_id": 1,
        "sender_username": "synthetic_peer", "create_time": 1700000000,
        "content": "", "sort_seq": 1, "subtype": "", "payload": {},
        "binding_status": "server_bound", "legacy_aliases": [{
            "scope": scope, "local_id": "1", "create_time": 1700000000,
            "sort_seq": 1, "identity": "local:1",
        }],
        "source_refs": [{
            "scope": scope,
            "source_key": {"relative_db": "message/message_0.db", "incarnation": "synthetic-file-1"},
            "table_name": "synthetic_messages", "local_id": "1",
            "expected_server_id": "9007199254740993", "create_time": 1700000000,
            "sort_seq": 1, "raw_local_type": 3,
            "storage_classes": ["integer", "integer", "integer", "integer", "integer"],
        }],
        "technical_observations": [{
            "raw_local_type": 3,
            "storage_classes": ["integer", "integer", "integer", "integer", "integer"],
        }],
        "media": {"status": "saved", "asset_name": "synthetic.png", "kind": "images"},
    })
    image = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
    )
    media = root / "assets" / "images" / "synthetic.png"
    media.parent.mkdir(parents=True)
    media.write_bytes(image)
    (root / "chat.md").write_text("# Synthetic capsule\n", encoding="utf-8")
    write_json(root / "capsule_meta.json", {
        "schema_version": 3, "message_count": 1, "scope": scope, "identity_contract_version": 1,
    })
    legacy = root / "snapshots" / "legacy_sample"
    legacy.mkdir(parents=True)
    (legacy / "chat.md").write_text("# Synthetic legacy sample\n", encoding="utf-8")
    write_json(legacy / "manifest.json", {"schema_version": 1, "files": {"chat.md": {
        "sha256": hashlib.sha256((legacy / "chat.md").read_bytes()).hexdigest(),
    }}})
    write_json(legacy / "export_meta.json", {"schema_version": 1, "message_count": 1})


def validate_contract_fixture(root):
    read = lambda relative: json.loads((root / relative).read_text(encoding="utf-8"))
    scope = {"account_id": "synthetic_account", "conversation_id": "synthetic_peer"}
    target, schema, state = read("target.json"), read("state/schema.json"), read("archive_state.json")
    assert target["schema_version"] == 2
    assert target["identity_contract"] == {"version": 1, "scope": scope, "origin_confirmed": True}
    index, capsule = read("snapshot_index.json"), read("capsule_meta.json")
    assert index["schema_version"] == 2
    assert schema["schema_version"] == state["schema_version"] == 3
    assert schema["format"] == "normalized-message-jsonl"
    assert schema["identity"] == "scoped-server-or-archive"
    assert schema["sort_keys"] == ["create_time", "sort_seq", "identity"]
    assert schema["source_locator_version"] == 1
    assert state["identity_contract_version"] == 1
    assert state["last_key"] == [1700000000, 1, "server:9007199254740993"]
    assert state["per_source_cursors"] == {}
    assert schema["scope"] == state["scope"] == capsule["scope"] == scope
    assert capsule["schema_version"] == 3 and capsule["identity_contract_version"] == 1
    rows = [json.loads(line) for line in (root / "state/messages.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert all(item["binding_status"] in {"server_bound", "archive_only"} for item in rows)
    row = rows[0]
    assert row["identity"] == "server:9007199254740993" and row["scope"] == scope
    assert row["binding_status"] == "server_bound"
    assert {"binding_status", "source_refs", "legacy_aliases", "technical_observations"} <= row.keys()
    assert row["legacy_aliases"] == [{
        "scope": scope, "local_id": "1", "create_time": 1700000000,
        "sort_seq": 1, "identity": "local:1",
    }]
    assert row["technical_observations"] == [{
        "raw_local_type": 3,
        "storage_classes": ["integer", "integer", "integer", "integer", "integer"],
    }]
    assert (root / "assets" / row["media"]["kind"] / row["media"]["asset_name"]).is_file()
    assert state["message_count"] == 1 and capsule["message_count"] == 1
    assert len(index["snapshots"]) == 1
    assert index["snapshots"][0]["id"] == "legacy_sample"
    assert index["snapshots"][0]["message_count"] == 1
    legacy_metadata = read("snapshots/legacy_sample/export_meta.json")
    assert legacy_metadata["message_count"] == 1
    return {
        "normalized_records": len(rows), "archive_state": state["message_count"],
        "capsule": capsule["message_count"], "legacy_index_sample": index["snapshots"][0]["message_count"],
        "legacy_snapshot_sample": legacy_metadata["message_count"],
    }


class FilesystemObserver:
    """Observe wrapped OS completions plus Python audit attempts, forwarding calls.

    Builtin write-capable opens are audit attempts only; this is not a universal
    successful-write counter. Genuine observed lock writes conclusively fail
    zero-write safety, but zero wrapped events alone cannot prove a global PASS.
    """
    def __init__(self, root):
        self.root = root.resolve()
        self.mutations = []
        self.boundary_attempts = []
        self.fds = {}
        self.active = False
        self.originals = {}

    def relative(self, value):
        if isinstance(value, int):
            return self.fds.get(value)
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        try:
            return Path(os.fsdecode(value)).resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None

    def audit(self, event, args):
        if not self.active:
            return
        operations = {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod",
                      "os.utime", "os.link", "os.symlink", "os.truncate"}
        if event == "open":
            flags = args[2]
            mode = args[1] or ""
            if not (any(letter in mode for letter in "wax+") or
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                return
        elif event not in operations:
            return
        for value in args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]:
            relative = self.relative(value)
            if relative is not None:
                self.boundary_attempts.append({"op": event, "path": relative})

    def install(self):
        sys.addaudithook(self.audit)
        for name in ("open", "write", "close", "unlink", "remove", "mkdir", "rmdir",
                     "rename", "replace", "truncate", "ftruncate", "chmod", "utime",
                     "link", "symlink"):
            original = getattr(os, name)
            self.originals[name] = original
            def observed(*args, _name=name, _original=original, **kwargs):
                relative = self.relative(args[0]) if args else None
                existed = (self.root / relative).exists() if relative is not None else False
                result = _original(*args, **kwargs)
                if _name == "open" and relative is not None:
                    self.fds[result] = relative
                if self.active and relative is not None:
                    is_mutation = _name != "close"
                    if _name == "open":
                        flags = args[1]
                        is_mutation = bool(flags & os.O_TRUNC or flags & os.O_CREAT and not existed)
                    if _name == "mkdir":
                        is_mutation = not existed
                    if is_mutation:
                        item = {"op": "os." + _name, "path": relative}
                        if _name == "write":
                            item["byte_count"] = result
                        self.mutations.append(item)
                if _name == "close":
                    self.fds.pop(args[0], None)
                return result
            setattr(os, name, observed)


def main():
    method, frozen_arg = sys.argv[1:]
    frozen = Path(frozen_arg).resolve()
    # -I excludes cwd/PYTHONPATH; only this explicit frozen root supplies package imports.
    sys.path.insert(0, str(frozen))
    manifest = json.loads((frozen / "SOURCE_HASHES.json").read_text(encoding="utf-8"))
    verified = {
        relative: hashlib.sha256((frozen / relative).read_bytes()).hexdigest()
        for relative in manifest["files"]
    }
    assert verified == manifest["files"], "immutable source hashes changed"
    service_module = importlib.import_module("wechat_exporter.service")
    from wechat_exporter.account import AccountChoice, BackendInfo
    from wechat_exporter.compatibility import (
        CapabilityProbeResult, ProbeErrorCode, RuntimeCapability,
        VersionVerification, WeChatCompatibility,
    )
    from wechat_exporter.registry import ArchiveRegistry
    from wechat_exporter.service import AppSettings, ExporterService
    # Load the separately frozen detector as a genuine package module; no algorithm substitution.
    detector_path = frozen.parent / "legacy_v2_migrations.py"
    detector_spec = importlib.util.spec_from_file_location("_legacy_v2_detector", detector_path)
    detector = importlib.util.module_from_spec(detector_spec)
    detector.__package__ = "wechat_exporter"
    # Distinct fixture alias permits dataclasses without contaminating frozen package origins.
    sys.modules[detector_spec.name] = detector
    detector_spec.loader.exec_module(detector)
    assert hashlib.sha256(detector_path.read_bytes()).hexdigest() == manifest["files"]["wechat_exporter/migrations.py"]
    class SyntheticDB:
        def __getattr__(self, name):
            raise AssertionError("SOURCE_BACKEND_MUST_NOT_BE_READ")
    with tempfile.TemporaryDirectory(prefix="synthetic_history_rc6_") as temporary:
        base = Path(temporary)
        root = base / "synthetic_target"
        make_archive(root)
        fixture_counts = validate_contract_fixture(root)
        settings_path = base / "synthetic_runtime" / "settings.json"
        write_json(settings_path, {
            "schema_version": 2, "archive_root": str(base / "empty_archives"),
            "known_archive_roots": [], "selected_account_identity": "",
        })
        registry = ArchiveRegistry(base / "synthetic_runtime" / "registry.json")
        registry.register(root)
        registry.mark_bootstrap_completed()
        choice = AccountChoice("synthetic_account", "synthetic_source", "synthetic", "synthetic", 0)
        source_calls = []
        def synthetic_database(stable_id):
            source_calls.append("synthetic")
            return SyntheticDB(), BackendInfo("1.2.1", "synthetic", "synthetic")
        service = ExporterService(
            base, settings=AppSettings(settings_path, base / "empty_exports"), registry=registry,
            account_lister=lambda: [choice], database_factory=synthetic_database,
            compatibility_provider=lambda: WeChatCompatibility(
                True, "4.1.13.65", VersionVerification.TESTED, "synthetic",
            ),
            probe_runner=lambda account: CapabilityProbeResult(RuntimeCapability.READY, ProbeErrorCode.NONE),
        )
        before = tree_hashes(root)
        # Independently characterize genuine detector on the same mixed v2/v3 contract.
        detector_error = None
        try:
            detector.detect_schema_version(root)
        except Exception as exc:
            detector_error = type(exc).__name__
        observer = FilesystemObserver(root)
        observer.install()
        refused = False
        error_type = None
        refusal_trace = []
        observer.active = True
        try:
            getattr(service, method)(root)
        except Exception as exc:
            refused = True
            error_type = type(exc).__name__
            frame = exc.__traceback__
            while frame is not None:
                filename = Path(frame.tb_frame.f_code.co_filename).resolve()
                try:
                    module_path = filename.relative_to(frozen).as_posix()
                except ValueError:
                    module_path = "_legacy_rc6_probe.py" if filename == Path(__file__).resolve() else "<runtime>"
                refusal_trace.append({"module": module_path, "function": frame.tb_frame.f_code.co_name})
                frame = frame.tb_next
        finally:
            observer.active = False
        after = tree_hashes(root)
        modules = {}
        for name, module in sorted(sys.modules.items()):
            if name == "wechat_exporter" or name.startswith("wechat_exporter."):
                path = Path(module.__file__).resolve()
                modules[name] = path.relative_to(frozen).as_posix()
        assert service_module.__file__ == str(frozen / "wechat_exporter" / "service.py")
        assert "wechatauto.db" not in sys.modules, "real backend imported"
        assert source_calls == [], "refusal occurred after source acquisition"
        print(json.dumps({
            "method": method, "role": "synthetic", "refused": refused,
            "error_type": error_type, "detector_error_type": detector_error,
            "refusal_trace": refusal_trace,
            "loaded_modules": modules, "source_hashes": verified,
            "before": before, "after": after, "final_tree_unchanged": before == after,
            "mutation_count": len(observer.mutations), "mutations": observer.mutations,
            "boundary_attempts": observer.boundary_attempts,
            "real_backend_imported": False, "source_calls": len(source_calls),
            "fixture_contract_verified": True,
            "fixture_counts": fixture_counts,
        }, sort_keys=True))
        # Test-only cleanup: genuine service owns a file logger on Windows.
        logging.shutdown()


if __name__ == "__main__":
    main()
