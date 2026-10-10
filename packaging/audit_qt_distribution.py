from __future__ import annotations

import argparse
import json
from importlib import metadata
from pathlib import Path

from release_gate import sha256_file, write_json


QT_DISTRIBUTIONS = ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6")
OFFICIAL_REFERENCES = (
    "https://doc.qt.io/qtforpython-6/",
    "https://doc.qt.io/qtforpython-6/licenses.html",
    "https://www.qt.io/development/open-source-lgpl-obligations",
)


def distribution_evidence(name: str) -> dict[str, object]:
    dist = metadata.distribution(name)
    license_files: list[str] = []
    for entry in dist.files or ():
        lowered = entry.name.casefold()
        if any(token in lowered for token in ("license", "licence", "copying", "notice")):
            located = Path(dist.locate_file(entry))
            if located.is_file():
                license_files.append(entry.as_posix())
    return {
        "canonical_name": dist.metadata.get("Name") or name,
        "version": dist.version,
        "license_expression": dist.metadata.get("License-Expression") or dist.metadata.get("License") or "",
        "license_files": sorted(set(license_files), key=str.casefold),
    }


def validate_qt_audit(payload: dict[str, object]) -> list[str]:
    issues: list[str] = []
    distributions = payload.get("distributions")
    distributions = distributions if isinstance(distributions, dict) else {}
    for name in QT_DISTRIBUTIONS:
        evidence = distributions.get(name)
        if not isinstance(evidence, dict):
            issues.append(f"missing distribution: {name}")
            continue
        if evidence.get("version") != "6.11.2":
            issues.append(f"wrong version: {name}")
        if not evidence.get("license_files"):
            issues.append(f"missing license files: {name}")
    if payload.get("dynamic_distribution") is not True:
        issues.append("Qt runtime is not recorded as dynamically distributed")
    if payload.get("upx") is not False:
        issues.append("UPX must remain disabled")
    for shipped in payload.get("shipped_qt_files", ()):
        basename = str(shipped).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if basename in {"qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll"}:
            issues.append(f"unused GPL-only Qt VirtualKeyboard shipped: {shipped}")
    return issues


def audit_release(release_directory: Path) -> dict[str, object]:
    release_directory = release_directory.resolve()
    internal = release_directory / "_internal"
    shipped = sorted(
        path.relative_to(release_directory).as_posix()
        for path in internal.rglob("*")
        if path.is_file() and (
            path.suffix.casefold() in {".dll", ".pyd"}
            or "plugins" in {part.casefold() for part in path.parts}
        ) and ("pyside6" in path.as_posix().casefold() or path.name.casefold().startswith("qt6"))
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "scope": "Qt technical inventory only; does not satisfy the complete distribution license gate",
        "qt_version": "6.11.2",
        "pyside_version": "6.11.2",
        "shiboken_version": "6.11.2",
        "distributions": {name: distribution_evidence(name) for name in QT_DISTRIBUTIONS},
        "dynamic_distribution": True,
        "onedir": True,
        "upx": False,
        "drm_or_replacement_restriction": False,
        "shipped_qt_files": shipped,
        "shipped_qt_file_count": len(shipped),
        "official_references": list(OFFICIAL_REFERENCES),
        "license_tree_sha256": {
            path.relative_to(release_directory).as_posix(): sha256_file(path)
            for path in sorted((release_directory / "THIRD_PARTY_LICENSES").rglob("*"))
            if path.is_file() and ("pyside" in path.as_posix().casefold() or "shiboken" in path.as_posix().casefold() or path.name in {"LGPL-3.0.txt", "GPL-3.0.txt", "QT_LGPL_REVIEW.txt"})
        },
        "legal_interpretation": "LEGAL_INTERPRETATION_REVIEW_RECOMMENDED",
        "license_distribution_check": "BLOCKED",
        "ffmpeg_corresponding_source": "BLOCKED",
    }
    issues = validate_qt_audit(payload)
    payload["issues"] = issues
    payload["status"] = "PASS" if not issues and shipped else "FAIL"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = audit_release(args.release_directory)
    write_json(args.output, payload)
    print(f"QT_LICENSE_AUDIT={payload['status']}")
    print(f"SHIPPED_QT_FILES={payload['shipped_qt_file_count']}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
