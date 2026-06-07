from PyQt6.QtWidgets import QMainWindow, QTabWidget

from .stock_tab     import StockScannerTab
from .options_tab   import OptionsScannerTab
from .lso_tab       import LsoAnalysisTab
from .positions_tab import PortfolioTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ThetaData Screener")
        self.resize(1100, 780)

        stock_tab   = StockScannerTab()
        options_tab = OptionsScannerTab()
        lso_tab     = LsoAnalysisTab()

        stock_tab.scan_finished.connect(options_tab.refresh_scanner_status)
        options_tab.scan_finished.connect(lso_tab.refresh_options_status)

        tabs = QTabWidget()
        tabs.addTab(stock_tab,      "Stock Scanner")
        tabs.addTab(options_tab,    "Options Scanner")
        tabs.addTab(lso_tab,        "LSO Analysis")
        tabs.addTab(PortfolioTab(), "Portfolio")
        self.setCentralWidget(tabs)
