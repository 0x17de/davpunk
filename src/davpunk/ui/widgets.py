"""Small shared widgets.

The form fields here need explaining — a CalDAV URL and a GPG key id are not
self-evident — but explaining them *inline* costs two or three wrapped lines per
field, which pushes a seven-field form past the height of its own dialog and
clips every hint.  So the explanation lives in a tooltip, behind a ``?`` badge
that says one is there.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

#: Outer size of the badge, in px; mirrored in the stylesheet below.
BADGE_SIZE = 14

#: Muted circle so it reads as an affordance rather than as content.
BADGE_STYLE = """
QLabel {
    color: palette(mid);
    border: 1px solid palette(mid);
    border-radius: 7px;
    min-width: 14px;
    max-width: 14px;
    min-height: 14px;
    max-height: 14px;
    font-size: 10px;
    font-weight: bold;
}
QLabel:hover {
    color: palette(highlight);
    border-color: palette(highlight);
}
"""


def help_badge(help_text: str, parent: QWidget | None = None) -> QLabel:
    """A ``?`` that shows ``help_text`` on hover."""
    badge = QLabel("?", parent)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setStyleSheet(BADGE_STYLE)
    badge.setCursor(Qt.CursorShape.WhatsThisCursor)
    badge.setToolTip(help_text)
    # Also answers Shift+F1, which is how the keyboard reaches it.
    badge.setWhatsThis(help_text)
    return badge


def help_label(text: str, help_text: str, parent: QWidget | None = None) -> QWidget:
    """A form label with a ``?`` badge beside it.

    Returned as a container rather than a styled QLabel so the badge is its own
    hover target; a tooltip on the whole row would fire anywhere near the text.
    """
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)

    label = QLabel(text, container)
    label.setToolTip(help_text)
    badge = help_badge(help_text, container)
    layout.addWidget(label)
    layout.addWidget(badge)

    # A plain QFormLayout label cannot be squeezed below its own text, but a
    # *container* can — and the QLabel inside then silently truncates
    # ("Encryption ke") once the dialog is narrow.  Pin the width the text
    # actually needs rather than trusting the propagated size hint.
    # sizeHint(), not fontMetrics().horizontalAdvance(): QLabel adds its own
    # frame margin, and being one pixel short still truncates.
    text_width = label.sizeHint().width()
    label.setMinimumWidth(text_width)
    container.setMinimumWidth(text_width + layout.spacing() + BADGE_SIZE)
    container.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
    container.setToolTip(help_text)
    return container


def hint(text: str = "", parent: QWidget | None = None) -> QLabel:
    """A muted note that is genuinely worth showing without a hover.

    Reserved for things the user has to *act* on — a validation problem, a key
    that was skipped — not for describing a field.
    """
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(mid); font-size: 11px;")
    return label


def apply_help(widget: QWidget, help_text: str) -> QWidget:
    """Put the same explanation on the input itself, so hovering it works too."""
    widget.setToolTip(help_text)
    widget.setWhatsThis(help_text)
    return widget
