"""One RC identity, validated before staging or collecting a release."""
from __future__ import annotations

import argparse
import ast
import json
import runpy
from pathlib import Path


VERSION = "0.3.1-rc.7"
DISPLAY_VERSION = "0.3.1 RC7"
RELEASE_TAG = "v0.3.1-rc7"
RELEASE_NAME = "微信时间胶囊-v0.3.1-rc7-win64"
FILE_VERSION = "0.3.1.0"


def load_release_config(source_root: Path, version_file: Path) -> dict[str, str]:
    application = runpy.run_path(str(source_root / "wechat_exporter" / "__init__.py"))
    if application.get("__version__") != VERSION or application.get("DISPLAY_VERSION") != DISPLAY_VERSION:
        raise ValueError("application version mismatch with RC7 release configuration")
    resource = ast.parse(version_file.read_text(encoding="utf-8-sig"))
    strings = {
        ast.literal_eval(node.args[0]): ast.literal_eval(node.args[1])
        for node in ast.walk(resource)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "StringStruct" and len(node.args) == 2
    }
    expected = {
        "ProductVersion": VERSION,
        "FileDescription": f"微信时间胶囊 v{DISPLAY_VERSION}",
        "FileVersion": FILE_VERSION,
    }
    if any(strings.get(key) != value for key, value in expected.items()):
        raise ValueError("Windows resource mismatch with RC7 release configuration")
    return {
        "version": VERSION,
        "display_version": DISPLAY_VERSION,
        "release_tag": RELEASE_TAG,
        "release_name": RELEASE_NAME,
        "file_version": FILE_VERSION,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("version_file", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(load_release_config(arguments.source_root, arguments.version_file), ensure_ascii=True))
