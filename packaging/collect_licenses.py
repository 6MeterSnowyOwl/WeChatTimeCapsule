from __future__ import annotations

import argparse
import shutil
import sys
from importlib import metadata
from pathlib import Path


RUNTIME_DISTRIBUTIONS = (
    "wechatauto-replica",
    "PySide6",
    "PySide6_Addons",
    "PySide6_Essentials",
    "shiboken6",
    "colorama",
    "cryptography",
    "cffi",
    "pycparser",
    "imageio-ffmpeg",
    "opencv-python",
    "numpy",
    "Pillow",
    "psutil",
    "pyperclip",
    "pywin32",
    "uiautomation",
    "comtypes",
    "winsdk",
    "zstandard",
)


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)


def license_files(dist: metadata.Distribution) -> list[Path]:
    result: list[Path] = []
    for entry in dist.files or ():
        lowered = entry.name.lower()
        if any(token in lowered for token in ("license", "licence", "copying", "notice", "authors", "copyright")):
            path = Path(dist.locate_file(entry))
            if path.is_file():
                result.append(path)
    return sorted(set(result), key=lambda item: str(item).lower())


def copy_component(target: Path, name: str) -> tuple[str, str, int]:
    dist = metadata.distribution(name)
    canonical = dist.metadata.get("Name") or name
    version = dist.version
    destination = target / f"{safe_name(canonical)}-{safe_name(version)}"
    destination.mkdir(parents=True, exist_ok=True)
    files = license_files(dist)
    if not files:
        raise RuntimeError(f"No packaged license text found for {canonical} {version}")
    for index, source in enumerate(files, 1):
        output_name = source.name
        if (destination / output_name).exists():
            output_name = f"{index:02d}_{output_name}"
        shutil.copyfile(source, destination / output_name)
    return canonical, version, len(files)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("--legal-dir", type=Path, required=True)
    args = parser.parse_args()

    target = args.target.resolve()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    rows: list[tuple[str, str, int]] = []
    for name in RUNTIME_DISTRIBUTIONS:
        rows.append(copy_component(target, name))

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python LICENSE.txt was not found")
    python_dir = target / f"Python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_dir.mkdir()
    shutil.copyfile(python_license, python_dir / "LICENSE.txt")
    rows.append(("Python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", 1))

    rows.append(copy_component(target, "PyInstaller"))

    for filename in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        source = args.legal_dir / filename
        if not source.is_file():
            raise RuntimeError(f"Required GNU license text missing: {source}")
        shutil.copyfile(source, target / filename)
    shutil.copyfile(args.legal_dir.parent / "QT_LGPL_REVIEW.txt", target / "QT_LGPL_REVIEW.txt")

    lines = [
        "微信时间胶囊 — Third-party licenses",
        "=====================================",
        "",
        "The application is Apache-2.0; each component below remains under its own license.",
        "Qt/PySide6 license options and the excluded GPL-only VirtualKeyboard are recorded in QT_LGPL_REVIEW.txt.",
        "Native Gyan FFmpeg 7.1 is GPL-3.0-or-later; its exact corresponding-source obligation is BLOCKED.",
        "LICENSE_DISTRIBUTION_CHECK=BLOCKED; local RC7 candidate only, not cleared for external distribution.",
        "PyAutoGUI and its optional dependency stack are excluded from this frozen application.",
        "",
        f"Component entries: {len(rows)}",
        "",
    ]
    lines.extend(f"- {name} {version}: {count} packaged license/notice file(s)" for name, version, count in rows)
    lines.extend(["", "No third-party license is relicensed under Apache-2.0.", ""])
    (target / "index.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Collected {len(rows)} component entries into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
