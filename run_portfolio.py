#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from gui.portfolio_window import PortfolioWindow


def main():
    app = QApplication(sys.argv)
    # Wayland/GNOME matches the running window to an installed .desktop file by
    # app_id (= this name); without it the dock shows a generic fallback icon.
    app.setDesktopFileName("tos-returns")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "bar-chart-increasing.png")))
    window = PortfolioWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
