from PyQt6.QtWidgets import QMainWindow

from .positions_tab import PortfolioTab


class PortfolioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Portfolio Tracker")
        self.resize(1100, 780)
        self.setCentralWidget(PortfolioTab())
