"""TEST ONLY. Fixed synthetic child, atomic job binding, bounded local JSON.

No product runner, discovery, capability wire, recovery or authority producer.
Win32 layouts follow the Microsoft SDK declarations; all handles are private.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT / ".venv-release-py312/Scripts/python.exe"
WORKER = PROJECT / "tests/_track2_native_worker.py"
MAX_JSON = 32768
INFINITE, WAIT_TIMEOUT, WAIT_FAILED = 0xffffffff, 258, 0xffffffff
PHASES = {"LEASE", "READER", "NATIVE_ENTRY", "SUCCESS", "REACQUIRED", "CREATED", "ERROR"}
PINNED_MEDIA_SHA256 = "e6446b534956f3461d9d8ff587d438ae1128aa0d2f8a35d9d1c3fbaeb25f8d3b"
_UNSAFE_REASON = None


class Blocked(RuntimeError):
    """Unsupported APIs or uncertain cleanup must never become a pass/skip."""


class CleanupUncertain(Blocked):
    """Typed failure: resource cleanup must not depend on exception wording."""


def admit_launch():
    if _UNSAFE_REASON:
        raise Blocked("PREVIOUS_CLEANUP_STOP")


def latch_stop():
    global _UNSAFE_REASON
    _UNSAFE_REASON = "OWNED_RESOURCE_CLEANUP_NOT_PROVEN"


class CleanupState:
    """Local ownership ledger, never transferred over diagnostic IPC.

Launch attempts start unknown. Only an OS no-child result or observed bounded
reaping resolves an entry. A failed close attempt permanently marks this run.
"""
    def __init__(self):
        self.pending = set()
        self.failed = False

    def started(self):
        token = object()
        self.pending.add(token)
        return token

    def resolved(self, token):
        self.pending.discard(token)

    def fail(self):
        self.failed = True
        latch_stop()

    @property
    def safe(self):
        return not self.pending and not self.failed


class ProtocolError(ValueError):
    pass


def valid_payload(phase, value):
    fields = {
        "LEASE": {"pid": int, "lock_id": dict},
        "READER": {"reader_pid": int},
        "NATIVE_ENTRY": {"fixture_relative": str, "baseline": dict, "wait": str, "pid": int},
        "SUCCESS": {"media_status": str, "baseline_unchanged": bool},
        "REACQUIRED": {"pid": int, "lock_id": dict},
        "CREATED": {"pid": int, "suspended": bool, "active": int, "supervisor_pid": int},
        "ERROR": {"type": str, "detail": str, "winerror": (int, type(None))},
    }[phase]
    if set(value) != set(fields) or any(type(value[key]) not in (kind if type(kind) is tuple else (kind,))
                                       for key, kind in fields.items()):
        return False
    if any(key in value and value[key] <= 0 for key in ("pid", "reader_pid", "supervisor_pid")):
        return False
    if "lock_id" in value:
        lock = value["lock_id"]
        if (set(lock) != {"volume_serial", "file_id_128", "link_count"}
                or type(lock["volume_serial"]) is not int or not 0 < lock["volume_serial"] < 2**64
                or type(lock["link_count"]) is not int or lock["link_count"] != 1
                or type(lock["file_id_128"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", lock["file_id_128"])):
            return False
    if phase == "NATIVE_ENTRY":
        relative = value["fixture_relative"]
        baseline = value["baseline"]
        if (not re.fullmatch(r"worker_tmp/synthetic-media-[a-z0-9_]+", relative)
                or value["wait"] != "WaitForSingleObject(INFINITE)"
                or set(baseline) != {"sources", "account"}):
            return False
        for tree in baseline.values():
            if type(tree) is not dict or not tree or len(tree) > 20:
                return False
            for path, digest in tree.items():
                if (type(path) is not str or Path(path).is_absolute() or ".." in Path(path).parts
                        or type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    return False
    if phase == "SUCCESS" and value["media_status"] != "success":
        return False
    if phase == "CREATED" and (value["suspended"] is not True or value["active"] != 1):
        return False
    return True


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def strict_json(data):
    if type(data) is not bytes or len(data) > MAX_JSON:
        raise ProtocolError("JSON_SIZE")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("DUPLICATE_KEY")
            result[key] = value
        return result
    try:
        return json.loads(data.decode("utf8"), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ProtocolError("NONFINITE")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProtocolError("INVALID_JSON") from error


def read_json(path):
    with Path(path).open("rb") as stream:
        return strict_json(stream.read(MAX_JSON + 1))


def write_json(path, value):
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf8")
    require(len(data) <= MAX_JSON, "JSON_SIZE")
    path = Path(path)
    pending = path.with_suffix(".pending")
    with pending.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    require(not path.exists(), "IPC_ALREADY_EXISTS")
    pending.replace(path)


class Channel:
    def __init__(self, root, operation, prefix="event"):
        self.root, self.operation, self.prefix = Path(root), operation, prefix
        self.sequence, self.cancelled, self.terminal = 0, False, False
        self.last_phase = None

    def emit(self, phase, payload):
        require(phase in PHASES and not self.terminal, "INVALID_EMIT")
        self.sequence += 1
        write_json(self.root / f"{self.prefix}-{self.sequence:03}.json",
                   {"operation": self.operation, "sequence": self.sequence,
                    "phase": phase, "payload": payload})
        self.terminal = phase in {"SUCCESS", "REACQUIRED", "ERROR"}

    def accept(self, document):
        if (type(document) is not dict or set(document) != {"operation", "sequence", "phase", "payload"}
                or document["operation"] != self.operation
                or type(document["sequence"]) is not int or document["sequence"] != self.sequence + 1
                or type(document["phase"]) is not str or document["phase"] not in PHASES or type(document["payload"]) is not dict
                or self.terminal or self.cancelled):
            raise ProtocolError("INVALID_TRANSITION")
        allowed = ({None: {"LEASE"}, "LEASE": {"READER"}, "READER": {"NATIVE_ENTRY", "SUCCESS"},
                    "NATIVE_ENTRY": set()} if self.prefix == "event" else
                   {None: {"CREATED"}} if self.prefix == "death" else {None: {"REACQUIRED"}})
        phase = document["phase"]
        if phase not in allowed.get(self.last_phase, set()) | {"ERROR"} or not valid_payload(phase, document["payload"]):
            raise ProtocolError("INVALID_PHASE_OR_PAYLOAD")
        self.sequence += 1
        self.last_phase = phase
        self.terminal = document["phase"] in {"SUCCESS", "REACQUIRED", "ERROR"}
        return document

    def next(self, deadline, process=None):
        path = self.root / f"{self.prefix}-{self.sequence + 1:03}.json"
        while time.monotonic() < deadline:
            if path.exists():
                result = self.accept(read_json(path))
                if result["phase"] == "ERROR":
                    raise Blocked("CHILD_ERROR:" + json.dumps(result["payload"], sort_keys=True))
                return result
            if process is not None and process.signaled():
                raise Blocked("CHILD_EXIT_BEFORE_ACK")
            time.sleep(.015)
        raise Blocked("BOUNDED_ACK_TIMEOUT")


class BASIC_LIMIT(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_longlong), ("PerJobUserTimeLimit", C.c_longlong),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", C.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class EXTENDED_LIMIT(C.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", C.c_size_t), ("JobMemoryLimit", C.c_size_t),
                ("PeakProcessMemoryUsed", C.c_size_t), ("PeakJobMemoryUsed", C.c_size_t)]


class ACCOUNTING(C.Structure):
    _fields_ = [(name, C.c_longlong) for name in ("TotalUserTime", "TotalKernelTime",
                "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                (name, W.DWORD) for name in ("TotalPageFaultCount", "TotalProcesses",
                "ActiveProcesses", "TotalTerminatedProcesses")]


class STARTUPINFO(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
                ("lpTitle", W.LPWSTR)] + [(name, W.DWORD) for name in
                ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars",
                 "dwFillAttribute", "dwFlags")] + [("wShowWindow", W.WORD),
                ("cbReserved2", W.WORD), ("lpReserved2", C.POINTER(W.BYTE)),
                ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", C.c_void_p)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


def kernel_api():
    require(os.name == "nt", "WINDOWS_REQUIRED")
    require(C.sizeof(C.c_void_p) == 8 and
            tuple(map(C.sizeof, (BASIC_LIMIT, EXTENDED_LIMIT, STARTUPINFO, STARTUPINFOEX, ACCOUNTING, PROCESS_INFORMATION)))
            == (64, 144, 104, 112, 48, 24), "FIXED_X64_SDK_LAYOUT_REQUIRED")
    k = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p,
                                       C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD,
                            C.c_void_p, W.LPCWSTR, C.POINTER(STARTUPINFOEX), C.POINTER(PROCESS_INFORMATION)], W.BOOL),
        "ResumeThread": ([W.HANDLE], W.DWORD),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "TerminateProcess": ([W.HANDLE, W.UINT], W.BOOL),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "IsProcessInJob": ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "CreateEventW": ([C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
        "GetCurrentProcess": ([], W.HANDLE),
        "GetHandleInformation": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(k, name)
        function.argtypes, function.restype = args, result
    return k


def checked(result, name):
    if not result:
        code = C.get_last_error()
        raise Blocked(f"{name}:WinError={code}:{C.FormatError(code).strip()}")
    return result


def close_handle(k, handle):
    if handle:
        checked(k.CloseHandle(handle), "CloseHandle")


def token_facts():
    k = kernel_api()
    adv = C.WinDLL("advapi32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    adv.OpenProcessToken.restype = W.BOOL
    adv.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    adv.GetTokenInformation.restype = W.BOOL
    handle, value, returned = W.HANDLE(), W.DWORD(), W.DWORD()
    checked(adv.OpenProcessToken(k.GetCurrentProcess(), 8, C.byref(handle)), "OpenProcessToken")
    try:
        checked(adv.GetTokenInformation(handle, 20, C.byref(value), 4, C.byref(returned)), "TokenElevation")
        require(returned.value == 4 and value.value == 0, "ELEVATED_TOKEN_REFUSED")
        checked(adv.GetTokenInformation(handle, 18, C.byref(value), 4, C.byref(returned)), "TokenElevationType")
        return {"elevated": False, "elevation_type": value.value}
    finally:
        close_handle(k, handle)


def identity(path):
    from wechat_exporter.physical_paths import prove_existing_root
    from wechat_exporter.track2_contracts import dto_document
    return dto_document(prove_existing_root(Path(path)).identity)


def validate_request(path):
    request = read_json(path)
    require(type(request) is dict and set(request) == {"operation", "root", "ids", "mode"}, "REQUEST_SHAPE")
    require(type(request["operation"]) is str and re.fullmatch(r"native-diagnostic-[0-9a-f]{32}", request["operation"]), "OPERATION_ID")
    require(request["mode"] in {"normal", "native", "pre_resume_death"}, "MODE")
    root = Path(request["root"])
    require(root.is_absolute() and Path(path) == root / "request.json", "REQUEST_ROOT")
    require(type(request["ids"]) is dict and set(request["ids"]) == {"root", "namespace", "legacy", "worker_tmp", "evidence"}, "REQUEST_IDENTITIES")
    for name, expected in request["ids"].items():
        target = root if name == "root" else root / name
        require(identity(target) == expected, "ROOT_ID_MISMATCH:" + name)
    return request


def fixed_command(request, role="worker"):
    require(role in {"worker", "reacquire", "death_supervisor"}, "ROLE")
    require(Path(sys.executable).resolve() == PYTHON.resolve() and sys.version_info[:3] == (3, 12, 14), "FIXED_PYTHON_REQUIRED")
    validate_request(request)
    return [str(PYTHON), "-I", "-B", "-S", "-X", "utf8", str(WORKER), str(request), role]


class NativeJob:
    def __init__(self, request, *, role="worker", fail_at=None, cleanup=None):
        admit_launch()  # Also protects callers that do not use run_diagnostic.
        # fail_at is test-only error injection at safe ownership boundaries.
        require(fail_at in {None, "after_job", "after_create"}, "FAULT_POINT")
        self.k, self.job, self.process, self.thread, self.pid = kernel_api(), None, None, None, None
        self.reaped = False
        self.cleanup = cleanup if cleanup is not None else CleanupState()
        self.creation_token = None
        command = fixed_command(request, role)
        attributes = None
        try:
            self.job = checked(self.k.CreateJobObjectW(None, None), "CreateJobObjectW")
            limits = EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            checked(self.k.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)), "SetInformationJobObject")
            if fail_at == "after_job":
                raise Blocked("INJECTED_AFTER_JOB")
            size = C.c_size_t()
            C.set_last_error(0)
            first = self.k.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
            require(not first and C.get_last_error() == 122 and 0 < size.value <= 65536,
                    f"InitializeAttributeSize:WinError={C.get_last_error()}")
            storage = C.create_string_buffer(size.value)
            checked(self.k.InitializeProcThreadAttributeList(storage, 1, 0, C.byref(size)), "InitializeAttributes")
            attributes = storage
            jobs = (W.HANDLE * 1)(self.job)
            checked(self.k.UpdateProcThreadAttribute(storage, 0, 0x0002000D, jobs,
                                                     C.sizeof(jobs), None, None), "JobListAttribute")
            startup, process = STARTUPINFOEX(), PROCESS_INFORMATION()
            startup.StartupInfo.cb = C.sizeof(STARTUPINFOEX)
            startup.lpAttributeList = C.cast(storage, C.c_void_p)
            environment = dict(os.environ)
            environment["TEMP"] = environment["TMP"] = str(Path(request).parent / "worker_tmp")
            env = C.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0")
            line = C.create_unicode_buffer(subprocess.list2cmdline(command))
            admit_launch()
            self.creation_token = self.cleanup.started()
            created = self.k.CreateProcessW(str(PYTHON), line, None, None, False,
                0x00080000 | 0x00000004 | 0x00000400 | 0x08000000,  # EXTENDED, SUSPENDED, UNICODE_ENV, NO_WINDOW
                env, str(Path(request).parent), C.byref(startup), C.byref(process))
            if not created:
                # Documented failed CreateProcess call creates no process.
                self.cleanup.resolved(self.creation_token)
            checked(created, "CreateProcessW_JOB_LIST")
            self.process, self.thread, self.pid = process.hProcess, process.hThread, process.dwProcessId
            for handle in (self.job, self.process, self.thread):
                flags = W.DWORD()
                checked(self.k.GetHandleInformation(handle, C.byref(flags)), "GetHandleInformation")
                require(not flags.value & 1, "INHERITABLE_OWNER_HANDLE")
            require(self.contains(self.process), "CREATION_JOB_MEMBERSHIP")
            if fail_at == "after_create":
                raise Blocked("INJECTED_AFTER_CREATE")
        except BaseException:
            try:
                self.close()
            finally:
                if not self.cleanup.safe:
                    self.cleanup.fail()
            raise
        finally:
            if attributes is not None:
                self.k.DeleteProcThreadAttributeList(attributes)

    def contains(self, handle):
        result = W.BOOL()
        checked(self.k.IsProcessInJob(handle, self.job, C.byref(result)), "IsProcessInJob")
        return bool(result.value)

    def active(self):
        info, returned = ACCOUNTING(), W.DWORD()
        checked(self.k.QueryInformationJobObject(self.job, 1, C.byref(info), C.sizeof(info), C.byref(returned)), "QueryJobAccounting")
        require(returned.value == C.sizeof(info), "ACCOUNTING_SIZE")
        return info.ActiveProcesses

    def resume(self):
        value = self.k.ResumeThread(self.thread)
        if value == 0xffffffff:
            checked(False, "ResumeThread")
        require(value == 1, "UNEXPECTED_SUSPEND_COUNT")

    def signaled(self):
        return process_signaled(self.k, self.process)

    def reap(self, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.signaled() and self.active() == 0:
                self.reaped = True
                self.cleanup.resolved(self.creation_token)
                return
            time.sleep(.015)
        raise Blocked("REAP_UNCERTAIN:process_signaled_AND_job_active_zero_not_observed")

    def cancel(self):
        checked(self.k.TerminateJobObject(self.job, 1223), "TerminateJobObject")
        self.reap()

    def exit_code(self):
        result = W.DWORD()
        checked(self.k.GetExitCodeProcess(self.process, C.byref(result)), "GetExitCodeProcess")
        return result.value

    def close(self):
        failure = None
        try:
            if self.process and not self.reaped:
                self.cancel()
        except BaseException as error:
            failure = error
        finally:
            # Close job even if observation failed: KILL_ON_CLOSE is last defense.
            for name in ("job", "thread", "process"):
                value = getattr(self, name)
                if value:
                    try:
                        close_handle(self.k, value)
                    except BaseException as error:
                        failure = failure or error
                    setattr(self, name, None)
        if failure:
            self.cleanup.fail()
            raise CleanupUncertain("NATIVE_JOB_CLEANUP:" + str(failure))


def process_signaled(k, handle):
    value = k.WaitForSingleObject(handle, 0)
    if value == WAIT_FAILED:
        checked(False, "WaitForSingleObject")
    require(value in {0, WAIT_TIMEOUT}, "UNEXPECTED_WAIT_RESULT")
    return value == 0


def open_observer(k, pid, *, terminate=False):
    require(type(pid) is int and pid > 0, "PID_SHAPE")
    return checked(k.OpenProcess(0x100000 | 0x1000 | (1 if terminate else 0), False, pid), "OpenProcess_Observer")


def tree_digests(root):
    # Only call with a validated operation-owned synthetic directory.
    root = Path(root)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def create_operation(mode):
    temp = tempfile.TemporaryDirectory(prefix="synthetic-native-diagnostic-")
    root = Path(temp.name)
    for name in ("namespace", "legacy", "worker_tmp", "evidence"):
        (root / name).mkdir()
    (root / "legacy/settings.json").write_bytes(b'{"known_archive_roots":[]}')
    (root / "legacy/archive_registry.json").write_bytes(b'{"archives":[],"bootstrap_completed":false}')
    request = {"operation": "native-diagnostic-" + uuid.uuid4().hex, "root": str(root), "mode": mode,
               "ids": {name: identity(root if name == "root" else root / name)
                       for name in ("root", "namespace", "legacy", "worker_tmp", "evidence")}}
    write_json(root / "request.json", request)
    return temp, root, request


def close_observers(k, observers, cleanup):
    errors = []
    for handle in observers:
        try:
            close_handle(k, handle)
        except BaseException as error:
            errors.append(error)
    if errors:
        cleanup.fail()
        raise CleanupUncertain("OBSERVER_CLOSE_FAILED:" + type(errors[0]).__name__)


def _run_media(root, request, facts, cleanup):
    channel = Channel(root / "evidence", request["operation"])
    job = NativeJob(root / "request.json", cleanup=cleanup)
    observers = []
    try:
        job.resume()
        deadline = time.monotonic() + 20
        lease = channel.next(deadline, job)
        require(lease["phase"] == "LEASE", "LEASE_ACK")
        # Windows venv python.exe may be a redirector. Observe the actual worker
        # process independently and prove it inherited this exact creation job.
        worker_pid = lease["payload"]["pid"]
        facts["lease_ack"] = lease
        worker_handle = open_observer(job.k, worker_pid)
        observers.append(worker_handle)
        require(job.contains(worker_handle) and not process_signaled(job.k, worker_handle), "WORKER_NOT_LIVE_IN_JOB")
        reader = channel.next(deadline, job)
        require(reader["phase"] == "READER", "READER_ACK")
        facts["reader_ack"] = reader
        reader_handle = open_observer(job.k, reader["payload"]["reader_pid"])
        observers.append(reader_handle)
        require(job.contains(reader_handle) and not process_signaled(job.k, reader_handle), "ACTUAL_READER_NOT_LIVE_IN_JOB")
        facts["reader_descendant_observed"] = True
        write_json(root / "evidence/reader-observed.json", {"operation": request["operation"],
            "sequence": 1, "phase": "READER_OBSERVED", "reader_pid": reader["payload"]["reader_pid"]})
        event = channel.next(deadline, job)
        facts["terminal_or_native_ack"] = event
        if request["mode"] == "native":
            require(event["phase"] == "NATIVE_ENTRY", "NATIVE_ACK")
            require(not job.signaled(), "NATIVE_ALREADY_RETURNED")
            facts.update(native_entry=True, contained_before=job.active())
            # A real competing lease attempt must fail while worker is in native code.
            from _track2_native_worker import make_profile
            from wechat_exporter.namespace_lock import authority_lock
            from wechat_exporter.track2_contracts import Track2Error
            profile = make_profile(root)
            try:
                with authority_lock(profile, "diagnostic-contender", timeout_seconds=.1):
                    raise Blocked("LEASE_NOT_HELD")
            except Track2Error as error:
                require(error.code == "WRITER_BUSY", "UNEXPECTED_LEASE_ERROR")
            facts["lease_held"] = True
            channel.cancelled = True
            job.cancel()
            facts["descendants_signaled"] = all(process_signaled(job.k, handle) for handle in observers)
            # Parent independently re-reads surviving synthetic sources after termination.
            source_root = root / event["payload"]["fixture_relative"]
            source_root.resolve(strict=True).relative_to(root / "worker_tmp")
            actual = {"sources": tree_digests(source_root / "sources"), "account": tree_digests(source_root / "account")}
            facts["baseline_unchanged"] &= actual == event["payload"]["baseline"]
            time.sleep(.2)  # Post-reap observation, never the native hang substitute.
            facts["no_late_write"] = not (root / "evidence/late-write").exists()
            facts["no_late_success"] = not (root / "evidence/event-004.json").exists()
        else:
            require(event["phase"] == "SUCCESS", "COMPLETION_ACK")
            job.reap()
            require(job.exit_code() == 0, "RETURNED_EXIT_CODE")
            facts.update(media_status=event["payload"]["media_status"])
            facts["baseline_unchanged"] &= event["payload"]["baseline_unchanged"]
        facts.update(process_signaled=job.signaled(), job_active_after=job.active(), worker_pid=worker_pid,
                     creation_pid=job.pid,
                     reader_pid=reader["payload"]["reader_pid"])
    finally:
        try:
            job.close()
        finally:
            close_observers(job.k, observers, cleanup)
    facts["owner_handles_closed"] = all(value is None for value in (job.job, job.thread, job.process))
    facts["observer_handles_closed"] = len(observers)
    if request["mode"] == "native":
        reacquire = NativeJob(root / "request.json", role="reacquire", cleanup=cleanup)
        try:
            reacquire.resume()
            result = Channel(root / "evidence", request["operation"], "reacquire").next(time.monotonic() + 10, reacquire)
            reacquire.reap()
            require(result["phase"] == "REACQUIRED" and reacquire.exit_code() == 0, "REACQUIRE_ACK")
            facts["fresh_process_reacquired_same_lock"] = (result["payload"]["lock_id"] == lease["payload"]["lock_id"]
                                                          and result["payload"]["pid"] != facts["worker_pid"])
            facts["reacquire_ack"] = result
        finally:
            reacquire.close()
        facts["reacquire_handles_closed"] = all(value is None for value in (reacquire.job, reacquire.thread, reacquire.process))


def _run_death(root, request, facts, cleanup=None):
    admit_launch()
    cleanup = cleanup if cleanup is not None else CleanupState()
    # Observer MUST NOT retain/duplicate a job handle: last-handle-close is tested.
    environment = dict(os.environ, TEMP=str(root / "worker_tmp"), TMP=str(root / "worker_tmp"))
    k, worker, supervisor = kernel_api(), None, None
    with (root / "evidence/death-output.log").open("wb") as output:
        command = fixed_command(root / "request.json", "death_supervisor")
        admit_launch()
        token = cleanup.started()
        try:
            helper = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, close_fds=True, creationflags=0x08000000)
        except BaseException as error:
            # Constructor failure does not supply independently observed child ownership.
            cleanup.fail()
            raise CleanupUncertain("SUPERVISOR_START_FAILED:" + type(error).__name__) from error
        try:
            event = Channel(root / "evidence", request["operation"], "death").next(time.monotonic() + 10)
            facts["creation_ack"] = event
            require(event["phase"] == "CREATED" and event["payload"]["suspended"] is True, "PRE_RESUME_ACK")
            supervisor = open_observer(k, event["payload"]["supervisor_pid"], terminate=True)
            require(not process_signaled(k, supervisor), "SUPERVISOR_ALREADY_DEAD")
            worker = open_observer(k, event["payload"]["pid"])
            require(not process_signaled(k, worker), "SUSPENDED_CHILD_ALREADY_DEAD")
            checked(k.TerminateProcess(supervisor, 1223), "TerminateProcess_ACTUAL_SUPERVISOR")
            facts["terminated_actual_supervisor"] = True
            helper.wait(5)
            end = time.monotonic() + 5
            while not process_signaled(k, worker) and time.monotonic() < end:
                time.sleep(.015)
            require(process_signaled(k, worker) and process_signaled(k, supervisor), "PRE_RESUME_DEATH_CLEANUP_UNCERTAIN")
            facts.update(creation_complete=True, resumed=False, supervisor_signaled=process_signaled(k, supervisor),
                         process_signaled=True, worker_never_started=not (root / "evidence/event-001.json").exists(),
                         worker_pid=event["payload"]["pid"], supervisor_pid=event["payload"]["supervisor_pid"],
                         supervisor_creation_pid=helper.pid)
        finally:
            errors = []
            helper_reaped = False
            try:
                helper_reaped = helper.poll() is not None
            except BaseException as error:
                errors.append(error)
            if not helper_reaped:
                try:
                    helper.kill()
                except BaseException as error:
                    errors.append(error)
                # Always attempt wait even when kill failed; neither skips handles.
                try:
                    helper.wait(5)
                    helper_reaped = True
                except BaseException as error:
                    errors.append(error)
            observed_dead = worker is not None and supervisor is not None
            for handle in (worker, supervisor):
                if handle:
                    try:
                        observed_dead = process_signaled(k, handle) and observed_dead
                    except BaseException as error:
                        observed_dead = False
                        errors.append(error)
                    finally:
                        try:
                            close_handle(k, handle)
                        except BaseException as error:
                            errors.append(error)
            try:
                helper._handle.Close()
            except BaseException as error:
                errors.append(error)
            if not errors and observed_dead and helper_reaped:
                cleanup.resolved(token)
            else:
                cleanup.fail()
                raise CleanupUncertain("DEATH_CLEANUP_NOT_PROVEN:" +
                    (type(errors[0]).__name__ if errors else "missing_process_observation"))
            facts["owner_handles_closed"] = True  # Actual job owner terminated; OS closes its handles.
            facts["observer_handles_closed"] = 3  # worker, actual owner, Popen redirector.


def run_diagnostic(mode):
    global _UNSAFE_REASON
    facts = {"status": "BLOCKED", "mode": mode, "product_NATIVE_TERMINATION": "UNKNOWN",
             "product_UI_RECOVERY": "UNKNOWN", "cleanup_complete": False}
    if _UNSAFE_REASON:
        facts["error"] = "PREVIOUS_CLEANUP_UNCERTAIN_STOP:" + _UNSAFE_REASON
        return facts
    temp = root = None
    cleanup = CleanupState()
    try:
        facts["token"] = token_facts()
        facts["pinned_media_sha256"] = PINNED_MEDIA_SHA256
        temp, root, request = create_operation(mode)
        facts["operation"] = request["operation"]
        facts["root_identity"] = request["ids"]["root"]
        baseline = tree_digests(root / "legacy")
        facts["baseline_unchanged"] = True
        if mode == "pre_resume_death":
            _run_death(root, request, facts, cleanup)
        else:
            _run_media(root, request, facts, cleanup)
            require(facts["process_signaled"] and facts["job_active_after"] == 0,
                    "REAP_POSTCONDITION")
            if mode == "native":
                require(facts["contained_before"] >= 2 and all(facts[name] for name in
                        ("native_entry", "lease_held", "reader_descendant_observed", "descendants_signaled",
                         "fresh_process_reacquired_same_lock", "no_late_write", "no_late_success")),
                        "NATIVE_POSTCONDITION")
        facts["baseline_unchanged"] &= baseline == tree_digests(root / "legacy")
        require(facts["baseline_unchanged"], "BASELINE_MUTATED")
        facts["status"] = "PASS"
    except BaseException as error:
        detail = str(error).replace(str(root), "<owned-temp>") if root is not None else str(error)
        facts["error"] = f"{type(error).__name__}:{detail}"
        if isinstance(error, CleanupUncertain):
            cleanup.fail()
    finally:
        if temp is not None:
            if cleanup.safe:
                try:
                    temp.cleanup()
                    facts["cleanup_complete"] = not root.exists()
                except OSError as error:
                    cleanup.fail()
                    facts.update(status="BLOCKED", error=f"TEMP_CLEANUP:WinError={error.winerror}")
            else:
                # Retain uncertain resources, do not traverse/delete a potentially live tree.
                temp._finalizer.detach()
                latch_stop()
                facts["retained_temporary_tree"] = True
                facts["unresolved_process_ownership"] = len(cleanup.pending)
        if facts["status"] == "PASS" and not facts["cleanup_complete"]:
            facts["status"] = "BLOCKED"
    return facts
