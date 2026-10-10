import os
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPEC).parent))
from release_config import load_release_config

stage = Path(os.environ["WTC_STAGE_ROOT"])
work = Path(os.environ["WTC_WORK_ROOT"])
version_file = Path(os.environ["WTC_VERSION_FILE"])
manifest_file = Path(os.environ["WTC_MANIFEST_FILE"])
release_config = load_release_config(stage, version_file)

datas = [(str(stage / "wechatauto_replica-1.2.1.dist-info"), "wechatauto_replica-1.2.1.dist-info")]
uia_spec = importlib.util.find_spec("uiautomation")
if uia_spec is None or not uia_spec.submodule_search_locations:
    raise RuntimeError("uiautomation package is missing from the release environment")
uia_bin = Path(next(iter(uia_spec.submodule_search_locations))) / "bin"
binaries = [
    (str(uia_bin / "UIAutomationClient_VC140_X64.dll"), "uiautomation/bin"),
    (str(uia_bin / "UIAutomationClient_VC140_X86.dll"), "uiautomation/bin"),
]

a = Analysis(
    [str(stage / "gui.py")],
    pathex=[str(stage)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "mouseinfo", "pyautogui", "pygetwindow", "pymsgbox", "pyrect",
        "pyscreeze", "pytweening", "tkinter", "PySide6.QtNetwork",
        "PySide6.QtVirtualKeyboard",
    ],
    noarchive=False,
    optimize=1,
)
# The Codex host adds document-tool DLLs to PATH. They are not application
# dependencies and can be ABI-incompatible with Windows' system ICU.
a.binaries = [entry for entry in a.binaries if "codex-runtimes" not in str(entry[1]).lower()]
# Qt Virtual Keyboard is unused by the Widgets GUI and GPL-only in Qt 6.11.
# Remove its native dependency/plugin explicitly; keep qwindows and native IME.
virtual_keyboard_files = {"qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll"}
a.binaries = [
    entry for entry in a.binaries
    if not any(str(value).replace("\\", "/").rsplit("/", 1)[-1].casefold() in virtual_keyboard_files for value in entry[:2])
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="微信时间胶囊",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    version=str(version_file),
    manifest=str(manifest_file),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=release_config["release_name"],
    contents_directory="_internal",
)
