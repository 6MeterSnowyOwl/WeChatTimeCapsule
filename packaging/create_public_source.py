from __future__ import annotations

import argparse
import json
import os
import stat
import zipfile
from pathlib import Path

from release_gate import DEFAULT_PRIVATE_PATTERNS, FORBIDDEN_DIRECTORY_NAMES, FORBIDDEN_EXTENSIONS, scan_bytes


ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def expand_allowlist(root: Path, entries: list[str]) -> list[Path]:
    files: set[Path] = set()
    for entry in entries:
        relative = Path(entry)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe allowlist entry: {entry}")
        path = root / relative
        parts = {part.casefold() for part in relative.parts}
        if parts & FORBIDDEN_DIRECTORY_NAMES or path.suffix.casefold() in FORBIDDEN_EXTENSIONS:
            raise ValueError(f"forbidden allowlist entry: {entry}")
        if path.is_dir():
            for candidate in path.rglob("*"):
                if not candidate.is_file():
                    continue
                candidate_relative = candidate.relative_to(root)
                candidate_parts = {part.casefold() for part in candidate_relative.parts}
                if candidate_parts & FORBIDDEN_DIRECTORY_NAMES:
                    continue
                if candidate.suffix.casefold() in FORBIDDEN_EXTENSIONS or candidate.suffix.casefold() in {".pyc", ".pyo"}:
                    continue
                files.add(candidate)
        elif path.is_file():
            files.add(path)
        else:
            raise FileNotFoundError(entry)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def create_public_source_zip(
    root: Path,
    destination: Path,
    entries: list[str],
    *,
    privacy_patterns: tuple[str, ...] = DEFAULT_PRIVATE_PATTERNS,
) -> dict[str, object]:
    root = root.resolve()
    files = expand_allowlist(root, entries)
    privacy_hits: list[dict[str, str]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        for pattern in privacy_patterns:
            if pattern in relative:
                privacy_hits.append({"path": relative, "pattern": pattern, "location": "path"})
        for hit in scan_bytes(path.read_bytes(), privacy_patterns):
            privacy_hits.append({"path": relative, "pattern": hit["pattern"], "location": hit["encoding"]})
    if privacy_hits:
        raise ValueError(f"privacy scan failed with {len(privacy_hits)} hit(s): {privacy_hits[:5]}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if path.suffix.casefold() in {".ps1", ".sh"} else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes())
    temporary.replace(destination)
    return {"status": "PASS", "file_count": len(files), "privacy_hits": 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("allowlist", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.allowlist.read_text(encoding="utf-8"))
    extra_patterns = tuple(json.loads(os.environ.get("WTC_PRIVATE_PATTERNS_JSON", "[]")))
    result = create_public_source_zip(
        args.root,
        args.destination,
        payload["entries"],
        privacy_patterns=DEFAULT_PRIVATE_PATTERNS + extra_patterns,
    )
    print(f"PUBLIC_SOURCE_FILES={result['file_count']}")
    print("PUBLIC_SOURCE_PRIVACY_HITS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
