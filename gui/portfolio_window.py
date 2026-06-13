from PyQt6.QtWidgets import QMainWindow, QTabWidget

from .positions_tab import PortfolioTab
from .gains_tab     import GainsTab


class PortfolioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Portfolio Tracker")
        self.resize(1100, 780)

        positions_tab = PortfolioTab()
        gains_tab     = GainsTab()

        positions_tab.csv_imported.connect(gains_tab.process_csv)

        tabs = QTabWidget()
        tabs.addTab(positions_tab, "Positions")
        tabs.addTab(gains_tab,     "P&L Chart")
        self.setCentralWidget(tabs)
