# Third-party corresponding source — v0.3.1 RC7

This is a technical distribution record, not legal advice. The application
Portable ZIP runs independently and does not require the source archives at runtime.
This local candidate is not cleared for external distribution:
`FFMPEG_CORRESPONDING_SOURCE=BLOCKED`, `LICENSE_DISTRIBUTION_CHECK=BLOCKED`,
`PUBLIC_RELEASE_READY=NO`.

| Component | Binary version | License route | Exact source archive | Official upstream origin | SHA-256 |
|---|---:|---|---|---|---|
| Qt | 6.11.2 | LGPL-3.0 / GPL-3.0 / commercial | `qt-everywhere-src-6.11.2.tar.xz` | `https://download.qt.io/official_releases/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz` | `6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33` |
| PySide6 / Shiboken6 | 6.11.2 | LGPL-3.0 / GPL-3.0 / commercial | `pyside-setup-everywhere-src-6.11.2.zip` | `https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.zip` | `c0fdd62b91a1d36d5ee2e1fb71050a32fbc93fcdeef0fdcb41d29afaaf00d9b5` |

For local RC7 staging these already-verified archives are reused as separate
source companion artifacts, outside the Portable ZIP. No
developer-machine absolute path is part of the record. The Qt/PySide runtime
DLL inventory is recorded separately in `qt-license-audit.json`.

The source-derived acknowledgement companion is `Qt-6.11.2-license-notices.zip`:
2,329 license/attribution files, 4,447,085 bytes, SHA-256
`7120ae6e76c23a0b53584e6e40d0d64998f104995c9e6f8c0152c45424800b2d`.
It was extracted from the hash-verified Qt source archive and includes notices
for all source modules, including PDFium/AGLFN. It does not imply every source
module is shipped in the Portable build. Exact full upstream source archives
remain separately available. Qt license options must be checked per actual
module; unused commercial/GPLv3-only VirtualKeyboard binaries are excluded.

## Native FFmpeg: corresponding source still missing

Runtime file: `_internal/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe`.
SHA-256: `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`.
Reported version: `7.1-essentials_build-www.gyan.dev`.
The native build uses `--enable-gpl --enable-version3 --enable-static` and
includes x264/x265/xvid/rubberband. It is GPL-3.0-or-later; the Python
imageio-ffmpeg wrapper's BSD-2-Clause license is a separate component license.

The executable is retained because HEVC image decoding needs it. Exact
corresponding source for this Gyan build and its linked dependencies has not
been staged. Generic FFmpeg upstream source alone does not close this item.
`FFMPEG_CORRESPONDING_SOURCE=BLOCKED`; the Qt/PySide archives, native binary
inventory and bundled license texts do not constitute an overall distribution PASS.
The actual RC7 delivered executable must be checked against the recorded hash.

`LEGAL_INTERPRETATION_REVIEW_RECOMMENDED`
