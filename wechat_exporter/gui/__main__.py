from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from wechat_exporter.service import ExporterService
from wechat_exporter import PRODUCT_NAME, __version__

from .main_window import MainWindow
from .theme import apply_application_theme


def run(project_root: Path | None = None) -> int:
    root = project_root or Path(__file__).resolve().parents[2]
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(__version__)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    apply_application_theme(app)
    window = MainWindow(ExporterService(root))
    window.show()
    QTimer.singleShot(0, window.show_onboarding_if_needed)
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(run())
