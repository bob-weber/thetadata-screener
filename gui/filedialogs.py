"""File dialogs that open where the last one of their kind was used.

A dialog given a bare filename (or nothing) opens in the working directory —
the program folder — every time, and Qt only remembers the last folder for
the session. These keep it in QSettings (~/.config/lso-tools/<app>.conf), one
folder per ``key``, so it survives restarts. A folder that no longer exists
is ignored.
"""

from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QFileDialog, QWidget

_ORG = "lso-tools"


def _last_dir(app: str, key: str) -> str:
    d = QSettings(_ORG, app).value(key, "", type=str)
    return d if d and Path(d).is_dir() else ""


def _remember(app: str, key: str, path: str) -> None:
    QSettings(_ORG, app).setValue(key, str(Path(path).parent))


def save_path(parent: QWidget, app: str, key: str, title: str,
              filename: str, filters: str) -> str:
    """Ask where to save ``filename``; '' if cancelled."""
    last = _last_dir(app, key)
    path, _ = QFileDialog.getSaveFileName(
        parent, title, str(Path(last) / filename) if last else filename, filters)
    if path:
        _remember(app, key, path)
    return path


def open_path(parent: QWidget, app: str, key: str, title: str, filters: str) -> str:
    """Ask for a file to open; '' if cancelled."""
    path, _ = QFileDialog.getOpenFileName(parent, title, _last_dir(app, key), filters)
    if path:
        _remember(app, key, path)
    return path
