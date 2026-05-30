from PyQt6.QtWidgets import QMainWindow, QTabWidget

from .stock_tab     import StockScannerTab
from .options_tab   import OptionsScannerTab
from .wheel_tab     import WheelAnalysisTab
from .positions_tab import PortfolioTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ThetaData Screener")
        self.resize(1100, 780)

        stock_tab   = StockScannerTab()
        options_tab = OptionsScannerTab()
        wheel_tab   = WheelAnalysisTab()

        stock_tab.scan_finished.connect(options_tab.refresh_scanner_status)
        options_tab.scan_finished.connect(wheel_tab.refresh_options_status)

        tabs = QTabWidget()
        tabs.addTab(stock_tab,      "Stock Scanner")
        tabs.addTab(options_tab,    "Options Scanner")
        tabs.addTab(wheel_tab,      "Wheel Analysis")
        tabs.addTab(PortfolioTab(), "Portfolio")
        self.setCentralWidget(tabs)
