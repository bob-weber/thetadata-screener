#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from gui.main_window import MainWindow

_DIR = Path(__file__).parent


def _run_script(name: str) -> None:
    subprocess.run(["bash", str(_DIR / name)], check=False)


def main():
    _run_script("start_terminal.sh")

    app = QApplication(sys.argv)
    app.aboutToQuit.connect(lambda: _run_script("stop_terminal.sh"))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
