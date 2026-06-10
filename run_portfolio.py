#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from gui.portfolio_window import PortfolioWindow


def main():
    app = QApplication(sys.argv)
    window = PortfolioWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
