from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wechat_exporter.archive import discover_targets
from wechat_exporter.registry import ArchiveRegistry
from wechat_exporter.service import AppSettings, ExporterService
from wechat_exporter.utils import write_json_atomic


def main(project_root: Path, output_path: Path) -> int:
    settings = AppSettings.default(project_root)
    registry_path = settings.app_data_dir / "archive_registry.json"
    registry = ArchiveRegistry(registry_path)

    # Construct only the local-reader slice: no compatibility provider, account
    # discovery, DB initialization, recovery, or online API is invoked.
    service = object.__new__(ExporterService)
    service.project_root = project_root
    service.settings = settings
    service.registry = registry

    entries = registry.entries()
    existing = sum(entry.path.is_dir() for entry in entries)
    targets = service.list_targets()
    discovery_bug = bool(existing and not targets)
    report = {
        "appdata_namespace": settings.app_data_dir.name,
        "registry_exists": registry_path.is_file(),
        "registry_target_count": len(entries),
        "registry_existing_path_count": existing,
        "local_target_count_with_online_layer_absent": len(targets),
        "project_source_export_target_count": len(
            discover_targets(project_root / "exports")
        ),
        "packaged_registry_discovery_bug": discovery_bug,
    }
    write_json_atomic(output_path, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 2 if discovery_bug else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify_registry_independence.py PROJECT_ROOT OUTPUT_PATH")
    raise SystemExit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
