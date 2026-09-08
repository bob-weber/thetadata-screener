"""A QGroupBox whose title collapses and expands its contents.

The Options Scanner stacks four boxes above its results table, and only the
results are worth looking at once a scan is running. Collapsing the rest is the
cheapest way to give that table the window.

Built on QGroupBox's own checkable machinery: Qt already toggles a checkable box
when you click anywhere in its title, so hiding the checkbox indicator leaves the
▾ / ▸ arrow in the title as the whole affordance, and the title bar as the button.
"""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGroupBox, QVBoxLayout, QWidget

_ARROW     = {True: "▾", False: "▸"}
_UNBOUNDED = 16777215   # QWIDGETSIZE_MAX — Qt's "no maximum"


class CollapsibleGroupBox(QGroupBox):
    """Group box that hides its contents when its title is clicked.

    Content widgets go in :meth:`content` rather than the box itself — one inner
    widget to show and hide keeps the box from having to know what's in it::

        box = CollapsibleGroupBox("Parameters")
        form = QFormLayout(box.content())
    """

    #: Emitted with the new state whenever the box opens or closes. Distinct from
    #: ``toggled``, which QGroupBox also uses for its checkable state.
    expanded_changed = pyqtSignal(bool)

    def __init__(self, title: str, expanded: bool = True, parent=None):
        super().__init__(parent)
        self._label   = title
        self._content = QWidget(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._content)

        self.setCheckable(True)
        # The indicator is the toggle Qt draws for a checkable box; zero-sizing it
        # leaves the click handling (title included) intact but drops the checkbox
        # look, which reads as "include this section" rather than "show it".
        self.setStyleSheet("QGroupBox::indicator { width: 0px; height: 0px; }")
        self.toggled.connect(self._apply)

        self.setChecked(expanded)
        self._apply(expanded)   # setChecked is a no-op when already in that state

    def content(self) -> QWidget:
        """The widget to parent content layouts to."""
        return self._content

    def is_expanded(self) -> bool:
        return self.isChecked()

    def _apply(self, expanded: bool):
        expanded = bool(expanded)
        self._content.setVisible(expanded)
        self.setTitle(f"{_ARROW[expanded]}  {self._label}")
        # Collapsed, the box is just its title: flat drops the frame that would
        # otherwise outline an empty strip, and pinning the height stops a
        # stretchy parent layout from handing that strip space anyway.
        self.setFlat(not expanded)
        self.updateGeometry()
        self.setMaximumHeight(_UNBOUNDED if expanded else self.sizeHint().height())
        self.expanded_changed.emit(expanded)
