"""Multi-shard inventory with one attempt deadline and retained frozen rows."""
from __future__ import annotations
import multiprocessing
from pathlib import Path
import pickle
from tempfile import TemporaryDirectory
import time
from .history_contracts import ArchiveScope, HistoryError, SourceCursor, SourceSetChanged
from .history_provider import capability_descriptor, open_frozen, validate_work_root
from .history_reader_worker import child_main, quote


class _Worker:
    def __init__(self, descriptor, directory, operation, args, grace):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=child_main,
            args=(child,descriptor,str(directory),operation,args),name="history-"+operation)
        self.grace = grace
        try:
            self.process.start()
        except BaseException:
            self.pipe.close()
            raise HistoryError("SOURCE_READ_FAILED") from None
        finally:
            child.close()

    def receive(self, deadline):
        pieces = []
        try:
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not self.pipe.poll(remaining):
                    raise HistoryError("SOURCE_READ_TIMEOUT")
                piece = self.pipe.recv_bytes(32768)
                if not piece:
                    break
                pieces.append(piece)
            kind, result = pickle.loads(b"".join(pieces))
        except (EOFError,OSError,pickle.UnpicklingError):
            raise HistoryError("SOURCE_READ_FAILED") from None
        if kind == "error":
            if result["code"] == "SOURCE_SET_CHANGED":
                raise SourceSetChanged(result)
            raise HistoryError(result["code"],result)
        return result

    def close(self, *, terminate=False):
        cleanup_deadline = time.monotonic()+self.grace
        def remaining():
            return max(0.,cleanup_deadline-time.monotonic())
        try:
            if self.process.is_alive() and not terminate:
                try:
                    self.pipe.send_bytes(b"close")
                except (OSError,BrokenPipeError):
                    pass
                self.process.join(min(.1,remaining()))
            if self.process.is_alive():
                self.process.terminate()
                # Reserve part of the single budget for kill/reap if needed.
                self.process.join(remaining()/2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(remaining())
            if self.process.is_alive():
                raise HistoryError("SOURCE_READ_TIMEOUT")
        finally:
            self.pipe.close()
            if not self.process.is_alive():
                self.process.close()


class ConversationReader:
    def __init__(self, db: object, scope: ArchiveScope, work_root: Path, *, page_size: int = 1000,
                 deadline_seconds: float = 120, worker_grace_seconds: float = 5):
        if type(page_size) is not int or page_size < 1 or deadline_seconds <= 0 or worker_grace_seconds <= 0:
            raise ValueError("INVALID_HISTORY_READER_OPTIONS")
        self.db, self.scope, self.work_root = db, scope, Path(work_root).absolute()
        self.page_size, self.deadline_seconds, self.worker_grace_seconds = page_size, deadline_seconds, worker_grace_seconds

    def open_inventory(self, *, mode: str, cursors: tuple[SourceCursor, ...] = ()) -> InventorySession:
        if mode not in ("full","quick"):
            raise HistoryError("INVENTORY_MODE_MISMATCH")
        deadline = time.monotonic()+self.deadline_seconds
        descriptor = capability_descriptor(self.db)
        if not getattr(descriptor,"account_id",None):
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "capability_binding", "branch": "reader_account_missing"})
        if descriptor.account_id != self.scope.account_id:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH")
        validate_work_root(descriptor,self.work_root)
        directory = TemporaryDirectory(prefix="history-operation-",dir=self.work_root)
        worker = session = None
        try:
            worker = _Worker(descriptor,Path(directory.name),"inventory",
                             (self.scope,mode,cursors,self.page_size),self.worker_grace_seconds)
            inventory, images, retained = worker.receive(deadline)
            session = InventorySession(inventory,images,retained,descriptor,directory,worker,
                                       deadline,self.worker_grace_seconds)
            session.assert_source_set_stable()
            return session
        except BaseException:
            if session is not None:
                session.close()
            else:
                if worker is not None:
                    worker.close(terminate=True)
                directory.cleanup()
            raise

    def probe_snapshots(self) -> dict:
        """One count-only private snapshot attempt; no row decode or archive write."""
        deadline = time.monotonic()+self.deadline_seconds
        descriptor = capability_descriptor(self.db)
        if not getattr(descriptor,"account_id",None):
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "capability_binding", "branch": "reader_account_missing"})
        if descriptor.account_id != self.scope.account_id:
            raise HistoryError("IDENTITY_SCOPE_MISMATCH")
        validate_work_root(descriptor,self.work_root)
        directory = TemporaryDirectory(prefix="history-operation-",dir=self.work_root)
        worker = None
        try:
            worker = _Worker(descriptor,Path(directory.name),"snapshot_probe",
                             (self.scope,),self.worker_grace_seconds)
            return worker.receive(deadline)
        finally:
            try:
                if worker is not None:
                    worker.close(terminate=True)
            finally:
                directory.cleanup()


class InventorySession:
    def __init__(self, inventory, images, retained, descriptor, directory, worker, deadline, grace):
        self.inventory = inventory
        self._retained, self._descriptor, self._directory = retained, descriptor, directory
        self._worker, self._deadline, self._grace = worker, deadline, grace
        self._connections = []
        self._closed = False
        try:
            for path in images:
                self._connections.append(open_frozen(Path(path)))
        except BaseException:
            for connection in self._connections:
                connection.close()
            self._connections.clear()
            raise HistoryError("SOURCE_READ_FAILED") from None

    def assert_source_set_stable(self) -> None:
        if self._closed:
            raise HistoryError("SOURCE_READ_FAILED")
        worker = None
        try:
            if time.monotonic() >= self._deadline:
                raise HistoryError("SOURCE_READ_TIMEOUT")
            with TemporaryDirectory(prefix="verification-",dir=self._directory.name) as root:
                try:
                    worker = _Worker(self._descriptor,Path(root),"verify",(self.inventory.sources,),self._grace)
                    worker.receive(self._deadline)
                finally:
                    if worker is not None:
                        worker.close(terminate=True)
        except BaseException:
            self.close()
            raise

    def resolve_raw_row(self, row_token: str) -> dict:
        if self._closed or row_token not in self._retained:
            raise HistoryError("SOURCE_READ_FAILED")
        index, table, local = self._retained[row_token]
        try:
            row = self._connections[index].execute(f"SELECT * FROM {quote(table)} WHERE local_id=?",(local,)).fetchone()
            if row is None:
                raise HistoryError("SOURCE_READ_FAILED")
            return dict(row)
        except Exception:
            raise HistoryError("SOURCE_READ_FAILED") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for connection in self._connections:
            connection.close()
        self._connections.clear()
        self._retained.clear()
        try:
            self._worker.close()
        finally:
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
