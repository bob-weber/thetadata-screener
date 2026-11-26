"""Keep text that changes at run time from widening the window.

A QLabel or QGroupBox title claims its full text width as a *minimum*, and Qt
grows a top-level window to fit its children's minimums. Status lines and
result titles are rewritten as scans run, so one long message — the LSO tab
listing every symbol missing from durable-tags.json on a single line — pushed
the screener past the screen edge, where its resize handles can't be reached,
and it could not be narrowed again.

``elastic()`` takes such widgets out of the width calculation: a label wraps
(and clips anything that can't wrap, such as a long path), a group box clips
its title. Use it only on text that changes; fixed captions are short and
should keep their size.
"""

from PyQt6.QtWidgets import QGroupBox, QLabel, QSizePolicy, QWidget


def elastic(*widgets: QWidget) -> None:
    for w in widgets:
        policy = w.sizePolicy()
        # Ignored: the layout gives the widget whatever width is free, and its
        # minimum is only an explicit minimumWidth (none), not its text.
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        w.setSizePolicy(policy)
        if isinstance(w, QLabel):
            w.setWordWrap(True)
        elif not isinstance(w, QGroupBox):
            raise TypeError(f"elastic() expects a QLabel or QGroupBox, got {type(w).__name__}")
