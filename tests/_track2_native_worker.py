"""Fixed TEST-ONLY bootstrap. Never accepts code, account, source or capabilities."""
from pathlib import Path
import sys

_PROJECT = Path(__file__).resolve().parents[1]
# Explicit trusted paths only. -S bypasses site/.pth, -I ignores ambient Python paths.
sys.path[:0] = [str(_PROJECT / "tests"), str(_PROJECT),
               str(_PROJECT / ".venv-release-py312/Lib/site-packages")]

import ctypes as C
import hashlib
import importlib.util
import os
import tempfile
import time
import types

from _track2_native_supervisor import (Channel, NativeJob, INFINITE, WAIT_FAILED, PINNED_MEDIA_SHA256,
    checked, close_handle, kernel_api, read_json, require, token_facts, tree_digests, validate_request)


def lock_identity(root):
    from track2_fixtures import native_handle_facts
    facts = native_handle_facts(root / "namespace/authority.lock")
    return {name: facts[name] for name in ("volume_serial", "file_id_128", "link_count")}


def make_profile(root):
    from wechat_exporter.physical_paths import prove_existing_root
    from wechat_exporter.track2_contracts import LegacyNamespaceProof
    from wechat_exporter.v3_profile import open_v3_profile
    old = root / "legacy"
    proof = LegacyNamespaceProof((old,), (),
        hashlib.sha256((old / "settings.json").read_bytes()).hexdigest(),
        hashlib.sha256((old / "archive_registry.json").read_bytes()).hexdigest(),
        ((old, prove_existing_root(old).identity),))
    return open_v3_profile(root / "namespace", "SYNTHETIC", proof)


class ExplicitSyntheticVerifier:
    def verify(self, base_db, scope):
        from wechat_exporter.track2_contracts import RuntimeProof
        require(type(base_db).__name__ == "SyntheticReaderDB", "SYNTHETIC_DB_REQUIRED")
        return RuntimeProof(scope, "test-native-synthetic", hashlib.sha256(b"synthetic-native-proof").hexdigest(), True)


def worker(request, channel):
    from history_fixtures import synthetic_media_history
    from wechat_exporter.history_runtime import verify_synthetic_runtime
    from wechat_exporter.namespace_lock import authority_lock, require_live_lease
    from wechat_exporter.message_identity import resolve_canonical_rows
    from wechat_exporter.source_media import export_record_media
    # Execute the pinned real media module without unrelated package GUI startup.
    # No downloader method is faked: its actual video route resolves/copies cache.
    package_path = _PROJECT / ".venv-release-py312/Lib/site-packages/wechatauto"
    require(hashlib.sha256((package_path / "media.py").read_bytes()).hexdigest() == PINNED_MEDIA_SHA256,
            "PINNED_MEDIA_HASH_MISMATCH")
    package = types.ModuleType("wechatauto")
    package.__path__ = [str(package_path)]
    sys.modules["wechatauto"] = package
    spec = importlib.util.spec_from_file_location("wechatauto.media", package_path / "media.py")
    pinned = importlib.util.module_from_spec(spec)
    sys.modules["wechatauto.media"] = pinned
    spec.loader.exec_module(pinned)
    MediaDownloader = pinned.MediaDownloader

    root = Path(request["root"])
    profile = make_profile(root)
    with authority_lock(profile, request["operation"], timeout_seconds=1) as lease:
        require_live_lease(profile, lease)
        channel.emit("LEASE", {"pid": os.getpid(), "lock_id": lock_identity(root)})
        with synthetic_media_history() as fixture:
            fixture.root.resolve(strict=True).relative_to(root / "worker_tmp")
            verify_synthetic_runtime(profile, fixture.db, fixture.scope, ExplicitSyntheticVerifier())
            reader = fixture.session._worker.process
            require(reader.is_alive() and reader.pid != os.getpid(), "READER_NOT_LIVE")
            channel.emit("READER", {"reader_pid": reader.pid})
            observed = root / "evidence/reader-observed.json"
            deadline = time.monotonic() + 10
            while not observed.exists() and time.monotonic() < deadline:
                time.sleep(.015)
            require(observed.exists(), "READER_OBSERVER_TIMEOUT")
            require(read_json(observed) == {"operation": request["operation"], "sequence": 1,
                    "phase": "READER_OBSERVED", "reader_pid": reader.pid}, "READER_OBSERVER_ACK")
            baseline = {"sources": tree_digests(fixture.root / "sources"),
                        "account": tree_digests(fixture.root / "account")}
            records = resolve_canonical_rows(fixture.session.inventory, ()).records
            record = next(item for item in records if item.source_refs[0].raw_local_type == 43)
            original_out = MediaDownloader._out
            event = None
            k = kernel_api()
            if request["mode"] == "native":
                event = checked(k.CreateEventW(None, True, False, None), "CreateEventW")
                initial = k.WaitForSingleObject(event, 0)
                if initial == WAIT_FAILED:
                    checked(False, "WaitForSingleObject_EVENT_INITIAL")
                require(initial == 258, "EVENT_NOT_UNSIGNALED")

                def native_out(downloader, save_dir, name):
                    # Called FROM the actual pinned download_video implementation,
                    # after locating a real synthetic cache payload, before its write.
                    Path(save_dir).resolve(strict=True).relative_to(fixture.root)
                    require_live_lease(profile, lease)
                    channel.emit("NATIVE_ENTRY", {"fixture_relative": fixture.root.relative_to(root).as_posix(),
                        "baseline": baseline, "wait": "WaitForSingleObject(INFINITE)", "pid": os.getpid()})
                    result = k.WaitForSingleObject(event, INFINITE)
                    if result == WAIT_FAILED:
                        checked(False, "WaitForSingleObject_NATIVE")
                    # Any unexpected return is visibly fatal; never usable success.
                    (root / "evidence/late-write").write_bytes(b"UNEXPECTED_NATIVE_RETURN")
                    raise RuntimeError("UNEXPECTED_NATIVE_RETURN:" + str(result))

                MediaDownloader._out = native_out
            try:
                result = export_record_media(fixture.session, record, fixture.db, fixture.output)
                require(request["mode"] == "normal", "NATIVE_RETURNED")
                actual = {"sources": tree_digests(fixture.root / "sources"),
                          "account": tree_digests(fixture.root / "account")}
                require(result.status == "success", "MEDIA_NOT_SUCCESS")
                require((fixture.output / result.relative_path).read_bytes() == fixture.expected[2], "MEDIA_BYTES")
                returned = {"media_status": result.status, "baseline_unchanged": baseline == actual}
            finally:
                MediaDownloader._out = original_out
                if event:
                    close_handle(k, event)
    # Success is only emitted after fixture's real reader/session/lease close.
    channel.emit("SUCCESS", returned)


def main():
    require(sys.version_info[:3] == (3, 12, 14), "FIXED_PYTHON_VERSION")
    require(len(sys.argv) == 3, "FIXED_ARGUMENT_COUNT")
    request = validate_request(Path(sys.argv[1]))
    root = Path(request["root"])
    # Must precede unchanged fixture construction; child crash resources stay owned.
    tempfile.tempdir = str(root / "worker_tmp")
    os.environ["TEMP"] = os.environ["TMP"] = tempfile.tempdir
    token_facts()
    role = sys.argv[2]
    require(role in {"worker", "reacquire", "death_supervisor"}, "ROLE")
    prefix = {"worker": "event", "reacquire": "reacquire", "death_supervisor": "death"}[role]
    channel = Channel(root / "evidence", request["operation"], prefix)
    try:
        if role == "worker":
            worker(request, channel)
        elif role == "reacquire":
            from wechat_exporter.namespace_lock import authority_lock, require_live_lease
            profile = make_profile(root)
            with authority_lock(profile, "diagnostic-reacquire", timeout_seconds=1) as lease:
                require_live_lease(profile, lease)
                facts = {"pid": os.getpid(), "lock_id": lock_identity(root)}
            channel.emit("REACQUIRED", facts)
        else:
            job = NativeJob(root / "request.json")
            event = None
            try:
                channel.emit("CREATED", {"pid": job.pid, "suspended": True, "active": job.active(),
                                         "supervisor_pid": os.getpid()})
                event = checked(job.k.CreateEventW(None, True, False, None), "CreateEventW_DEATH")
                value = job.k.WaitForSingleObject(event, INFINITE)
                if value == WAIT_FAILED:
                    checked(False, "WaitForSingleObject_DEATH")
                raise RuntimeError("DEATH_WAIT_UNEXPECTED_RETURN")
            finally:
                if event:
                    close_handle(job.k, event)
                job.close()
    except BaseException as error:
        if not channel.terminal:
            channel.emit("ERROR", {"type": type(error).__name__, "detail": str(error)[:4000],
                                   "winerror": getattr(error, "winerror", None)})
        raise


if __name__ == "__main__":
    main()
