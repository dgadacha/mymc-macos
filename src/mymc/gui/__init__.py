"""Qt user interface for mymc.

Importing this package requires PySide6; :func:`run` is the entry point
used by both the ``mymc gui`` command and the ``mymc-gui`` script.
"""

import os
import sys

__all__ = ["run"]

_SAVE_SUFFIXES = (".psu", ".max", ".cbs", ".sps", ".xps")


def _make_file_open_filter(window):
    """Build the event filter for the Apple Event macOS sends on open.

    Double-clicking a card image or a save file in the Finder, or dropping
    one on the Dock icon, arrives here rather than on the command line.
    The class is defined lazily so that importing this module does not
    require PySide6.
    """
    from PySide6.QtCore import QEvent, QObject

    class FileOpenFilter(QObject):
        def __init__(self, window):
            super().__init__()
            self.window = window

        def eventFilter(self, obj, event):
            if event.type() == QEvent.FileOpen:
                path = event.file()
                if path:
                    self.open_path(path)
                    return True
            return False

        def open_path(self, path):
            if os.path.splitext(path)[1].lower() in _SAVE_SUFFIXES:
                if self.window.mc is not None:
                    self.window.import_files([path])
            else:
                self.window.open_image(path)

    return FileOpenFilter(window)


def run(filename=None):
    """Display the graphical interface, optionally opening an image."""
    from PySide6.QtWidgets import QApplication

    from .appicon import app_icon
    from .mainwindow import MainWindow

    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("mymc")
    app.setApplicationDisplayName("mymc")
    app.setOrganizationName("mymc")
    app.setOrganizationDomain("mymc.local")
    app.setWindowIcon(app_icon())

    window = MainWindow(filename)

    handler = _make_file_open_filter(window)
    app.installEventFilter(handler)
    window._file_open_filter = handler  # keep a reference so it stays alive

    window.show()
    window.raise_()
    window.activateWindow()

    return app.exec() if owns_app else 0
