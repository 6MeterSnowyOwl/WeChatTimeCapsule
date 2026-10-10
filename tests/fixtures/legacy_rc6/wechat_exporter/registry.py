from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .utils import write_json_atomic


REGISTRY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RegistryEntry:
    archive_id: str
    path: Path


class ArchiveRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.recovery_notice = ""
        self.data = self._load()
        if not self.path.is_file():
            self.save()

    def _default(self) -> dict:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "archives": [],
            "bootstrap_completed": False,
        }

    def _load(self) -> dict:
        if not self.path.is_file():
            return self._default()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("archives"), list):
                raise ValueError("invalid registry")
            if int(raw.get("schema_version", 0)) > REGISTRY_SCHEMA_VERSION:
                raise RuntimeError("archive registry was created by a newer version")
            return raw
        except RuntimeError:
            raise
        except (OSError, ValueError, TypeError):
            backup = self.path.with_name(
                f"{self.path.name}.corrupt.{datetime.now():%Y%m%d_%H%M%S}"
            )
            try:
                self.path.replace(backup)
                self.recovery_notice = "归档登记表损坏，已恢复为空登记表。已有聊天备份没有被修改。"
            except OSError:
                self.recovery_notice = "归档登记表损坏，已使用临时默认值。已有聊天备份没有被修改。"
            return self._default()

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    def entries(self) -> list[RegistryEntry]:
        result = []
        for raw in self.data.get("archives", []):
            try:
                archive_id = str(raw["archive_id"])
                path = Path(str(raw["path"])).resolve()
            except (KeyError, OSError, ValueError, TypeError):
                continue
            result.append(RegistryEntry(archive_id, path))
        return result

    def register(self, path: Path) -> RegistryEntry:
        resolved = path.resolve()
        for entry in self.entries():
            if entry.path == resolved:
                return entry
        entry = RegistryEntry(uuid.uuid4().hex, resolved)
        self.data.setdefault("archives", []).append(
            {"archive_id": entry.archive_id, "path": str(entry.path)}
        )
        self.save()
        return entry

    @property
    def bootstrap_completed(self) -> bool:
        return bool(self.data.get("bootstrap_completed", False))

    def mark_bootstrap_completed(self) -> None:
        if self.bootstrap_completed:
            return
        updated = dict(self.data)
        updated["bootstrap_completed"] = True
        write_json_atomic(self.path, updated)
        self.data = updated

    def entry_for_path(self, path: Path) -> RegistryEntry | None:
        resolved = path.resolve()
        return next((entry for entry in self.entries() if entry.path == resolved), None)

    def remove(self, archive_id: str) -> bool:
        archives = list(self.data.get("archives", []))
        retained = [
            raw for raw in archives
            if str(raw.get("archive_id") or "") != archive_id
        ]
        if len(retained) == len(archives):
            return False
        updated = dict(self.data)
        updated["archives"] = retained
        write_json_atomic(self.path, updated)
        self.data = updated
        return True

    def id_for_path(self, path: Path) -> str:
        return self.register(path).archive_id
