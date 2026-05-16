from PyQt6.QtWidgets import QMainWindow, QTabWidget

from .stock_tab   import StockScannerTab
from .options_tab import OptionsScannerTab
from .wheel_tab   import WheelAnalysisTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ThetaData Screener")
        self.resize(1100, 780)

        tabs = QTabWidget()
        tabs.addTab(StockScannerTab(),   "Stock Scanner")
        tabs.addTab(OptionsScannerTab(), "Options Scanner")
        tabs.addTab(WheelAnalysisTab(),  "Wheel Analysis")
        self.setCentralWidget(tabs)
