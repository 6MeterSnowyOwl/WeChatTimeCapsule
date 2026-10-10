from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class TargetBusyError(RuntimeError):
    pass


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class LockInfo:
    pid: int
    created_at: str
    operation_id: str
    operation: str
    target_hash: str


class TargetOperationLock:
    """Cross-process lock for one archive target, with PID-based stale recovery."""

    def __init__(
        self,
        target_root: Path,
        operation: str,
        *,
        process_checker: Callable[[int], bool] = process_exists,
    ):
        self.target_root = target_root.resolve()
        self.path = self.target_root / ".operation.lock"
        self.operation = operation
        self.process_checker = process_checker
        self.info = LockInfo(
            pid=os.getpid(),
            created_at=datetime.now(timezone.utc).isoformat(),
            operation_id=uuid.uuid4().hex,
            operation=operation,
            target_hash=hashlib.sha256(str(self.target_root).encode("utf-8")).hexdigest()[:16],
        )
        self.acquired = False

    def acquire(self) -> LockInfo:
        self.target_root.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if not self._recover_stale_lock():
                    raise TargetBusyError("该备份对象正在执行另一项操作，请等待完成。")
                continue
            try:
                payload = json.dumps(self.info.__dict__, ensure_ascii=False).encode("utf-8")
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.acquired = True
            return self.info
        raise TargetBusyError("无法安全取得归档操作锁。")

    def _recover_stale_lock(self) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            owner_pid = int(raw.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return False
        if self.process_checker(owner_pid):
            return False
        resolved = self.path.resolve()
        resolved.relative_to(self.target_root)
        try:
            resolved.unlink()
        except FileNotFoundError:
            pass
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("operation_id") != self.info.operation_id:
                return
            self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> LockInfo:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
