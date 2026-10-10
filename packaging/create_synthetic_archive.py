from __future__ import annotations

import argparse
import base64
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from wechat_exporter.integrity import generate_current_manifest, verify_current_capsule
from wechat_exporter import __version__


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    root = args.target.resolve()
    if root.exists():
        shutil.rmtree(root)
    (root / "assets" / "avatars").mkdir(parents=True)
    (root / "assets" / "images").mkdir(parents=True)
    (root / "state").mkdir(parents=True)

    (root / "assets" / "avatars" / "contact.png").write_bytes(PNG_1X1)
    (root / "assets" / "images" / "example.png").write_bytes(PNG_1X1)
    (root / "chat.md").write_text(
        "# Synthetic Contact\n\n"
        "A non-private acceptance fixture.\n\n"
        "## 2026-01-01\n\n"
        "**Synthetic Contact** · 12:00\n\n"
        "Hello from the synthetic archive.\n\n"
        "**我** · 12:01\n\n"
        "![Synthetic image](assets/images/example.png)\n",
        encoding="utf-8",
    )
    write_json(root / "capsule_meta.json", {
        "product_version": __version__, "capsule_schema_version": 1,
        "last_saved_at": "2026-01-01T12:01:00+00:00", "last_save_mode": "synthetic",
        "message_count": 2, "first_message_time": "2026-01-01T12:00:00+00:00",
        "last_message_time": "2026-01-01T12:01:00+00:00", "image_count": 1,
        "video_count": 0, "avatar_history_count": 1, "legacy_snapshot_count": 0,
        "avatars": {"contact": {"path": "assets/avatars/contact.png"}},
        "integrity_status": "complete",
    })
    write_json(root / "target.json", {
        "schema_version": 2, "username": "synthetic_contact_001",
        "display_name": "Synthetic Contact",
    })
    write_json(root / "archive_state.json", {
        "schema_version": 2, "message_count": 2, "last_sort_seq": 2,
        "last_message_identity": "synthetic:2", "last_sort_key": [2, 2],
    })
    write_json(root / "snapshot_index.json", {"schema_version": 2, "snapshots": []})
    write_json(root / "state" / "schema.json", {
        "schema_version": 2, "format": "wechat-exporter-normalized-message-jsonl",
        "identity": "local_id", "sort_key": ["sort_seq", "create_time", "local_id"],
        "content_model": "structured-v2",
    })
    (root / "state" / "messages.jsonl").write_text("", encoding="utf-8")
    generate_current_manifest(root, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = verify_current_capsule(root)
    if not result.ok:
        raise RuntimeError(f"Synthetic archive integrity failed: {len(result.issues)} issue(s)")
    print("SYNTHETIC_ARCHIVE_INTEGRITY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
