from __future__ import annotations

import argparse
import hashlib
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from release_gate import sha256_file


@dataclass(frozen=True)
class SourceArchive:
    component: str
    version: str
    license: str
    filename: str
    url: str
    size: int
    sha256: str


SOURCE_ARCHIVES = (
    SourceArchive(
        "Qt", "6.11.2", "LGPL-3.0/GPL-3.0/commercial",
        "qt-everywhere-src-6.11.2.tar.xz",
        "https://download.qt.io/official_releases/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz",
        1_019_661_552,
        "6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33",
    ),
    SourceArchive(
        "PySide6 / Shiboken6", "6.11.2", "LGPL-3.0/GPL-3.0/commercial",
        "pyside-setup-everywhere-src-6.11.2.zip",
        "https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.zip",
        23_134_400,
        "c0fdd62b91a1d36d5ee2e1fb71050a32fbc93fcdeef0fdcb41d29afaaf00d9b5",
    ),
)


def verify_source_archive(path: Path, expected_sha256: str, expected_size: int) -> dict[str, object]:
    actual_size = path.stat().st_size
    actual_sha256 = sha256_file(path)
    status = "PASS" if actual_size == expected_size and actual_sha256 == expected_sha256 else "FAIL"
    return {
        "status": status,
        "filename": path.name,
        "size": actual_size,
        "sha256": actual_sha256,
        "size_matches": actual_size == expected_size,
        "sha256_matches": actual_sha256 == expected_sha256,
    }


def inspect_source_archive(path: Path, version: str) -> dict[str, object]:
    names: list[str]
    lowered = path.name.casefold()
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"corrupt ZIP member: {bad}")
            names = archive.namelist()
        archive_type = "zip"
    elif lowered.endswith(".tar.xz"):
        with tarfile.open(path, "r:xz") as archive:
            names = archive.getnames()
        archive_type = "tar.xz"
    else:
        raise ValueError(f"unsupported source archive: {path.name}")
    marker = any(version in name for name in names[:1000])
    if not names or not marker:
        raise ValueError(f"archive does not expose version marker {version}")
    return {
        "archive_type": archive_type,
        "entry_count": len(names),
        "version_marker_found": marker,
        "first_entries": names[:20],
        "sha256": sha256_file(path),
    }


def download_resumable(item: SourceArchive, destination: Path) -> None:
    part = destination.with_suffix(destination.suffix + ".part")
    start = part.stat().st_size if part.exists() else 0
    if destination.exists() and verify_source_archive(destination, item.sha256, item.size)["status"] == "PASS":
        return
    request = urllib.request.Request(item.url)
    if start:
        request.add_header("Range", f"bytes={start}-")
    with urllib.request.urlopen(request, timeout=60) as response:
        mode = "ab" if start and response.status == 206 else "wb"
        if mode == "wb":
            start = 0
        with part.open(mode) as handle:
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    result = verify_source_archive(part, item.sha256, item.size)
    if result["status"] != "PASS":
        raise RuntimeError(f"source archive verification failed: {item.filename}")
    part.replace(destination)


def write_indexes(target: Path, inspections: list[dict[str, object]]) -> None:
    by_name = {str(item["filename"]): item for item in inspections}
    source_lines = [
        "WeChat Time Capsule third-party corresponding source", "================================================", "",
        "Technical distribution record; not legal advice.",
        "These exact upstream archives are staged separately from the application binary ZIP.", "",
    ]
    hash_lines: list[str] = []
    for item in SOURCE_ARCHIVES:
        inspection = by_name[item.filename]
        source_lines.extend([
            f"Component: {item.component}", f"Binary version: {item.version}",
            f"License route: {item.license}", f"Archive: {item.filename}",
            f"Official origin: {item.url}", f"SHA-256: {item.sha256}",
            f"Bytes: {inspection['size']}", f"Archive entries: {inspection['entry_count']}",
            "Validation: PASS", "",
        ])
        hash_lines.append(f"{item.sha256}  {item.filename}")
    for filename, lines in (("SOURCE_INDEX.txt", source_lines), ("SHA256SUMS.txt", hash_lines)):
        temporary = target / f"{filename}.part"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(target / filename)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    target = args.target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    inspections: list[dict[str, object]] = []
    for item in SOURCE_ARCHIVES:
        path = target / item.filename
        if args.download:
            download_resumable(item, path)
        if not path.is_file():
            raise FileNotFoundError(item.filename)
        verified = verify_source_archive(path, item.sha256, item.size)
        if verified["status"] != "PASS":
            raise RuntimeError(f"source archive verification failed: {item.filename}")
        inspected = inspect_source_archive(path, item.version)
        inspections.append({**verified, **inspected})
        print(f"SOURCE_ARCHIVE_PASS={item.filename}")
    write_indexes(target, inspections)
    print(f"SOURCE_TOTAL_BYTES={sum(int(item['size']) for item in inspections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

