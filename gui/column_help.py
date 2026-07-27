"""Shared per-column help for the screener tables.

Each tab owns a ``{column key: help text}`` map. :func:`install` wires that map
to a table three ways — a header tooltip for a passing glance, and a right-click
popup on either the header or a cell for the full text. Cells are included
because that's where the eye already is when a number needs explaining, and a
column with no entry in the map simply has no help rather than an empty popup.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox, QTableWidget


def _show(table: QTableWidget, col: int, help_map: dict, parent):
    """Pop up the help for one column, if it has any."""
    if col < 0 or col >= table.columnCount():
        return
    key = getattr(table, "_help_cols", [])
    if col >= len(key):
        return
    text = help_map.get(key[col])
    if not text:
        return
    hdr  = table.horizontalHeaderItem(col)
    name = hdr.text() if hdr is not None else key[col]

    box = QMessageBox(parent or table)
    box.setWindowTitle(f"Column: {name}")
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(name)
    box.setInformativeText(text)
    box.setIcon(QMessageBox.Icon.NoIcon)
    box.setStandardButtons(QMessageBox.StandardButton.Close)
    box.exec()


def install(table: QTableWidget, cols: list[str], help_map: dict, parent=None):
    """Attach ``help_map`` to ``table``: header tooltips + right-click popups.

    ``cols`` is the tab's column-key list, positionally matching the table's
    columns. Keying help by name rather than index means reordering or dropping
    a column can't silently misalign the explanations.
    """
    table._help_cols = list(cols)

    for i, key in enumerate(cols):
        item = table.horizontalHeaderItem(i)
        if item is not None and key in help_map:
            item.setToolTip(help_map[key] + "\n\n(right-click for a readable popup)")

    header = table.horizontalHeader()
    header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    header.customContextMenuRequested.connect(
        lambda pos: _show(table, header.logicalIndexAt(pos.x()), help_map, parent)
    )

    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.customContextMenuRequested.connect(
        lambda pos: _show(table, table.columnAt(pos.x()), help_map, parent)
    )
