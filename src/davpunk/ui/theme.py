"""The application palette, and the density that goes with it.

Everything the UI already styles asks for a *role* — ``palette(mid)`` in
:mod:`davpunk.ui.widgets`, ``palette(highlight)`` in the kanban headers,
``QPalette.ColorRole.AlternateBase`` in the merge dialog.  Nothing anywhere
names a colour.  So a theme here is one :class:`QPalette` and no per-widget
edits: define the roles, and every existing stylesheet string follows.

That only holds under the **Fusion** style.  A native platform style paints
from the desktop's own theme and quietly ignores an application palette for
most roles, which is why ``theme = "dark"`` on a light GTK desktop has never
produced a dark window.  Fusion honours the palette completely and renders the
same on every desktop, which is what a Wayland-primary app wants anyway: there
may be no GTK theme installed to inherit from.
"""

from __future__ import annotations

import logging

from PySide6.QtGui import QColor, QPalette

log = logging.getLogger("davpunk.ui.theme")

#: The one accent, and the same value the config reference gives as a remote's
#: default colour — the highlight and a list's own colour should not disagree
#: about what "DavPunk blue" is.
ACCENT_DARK = "#4A9EFF"

#: Darkened for a white background: #4A9EFF on white is under 3:1.
ACCENT_LIGHT = "#2F7FE0"

#: Role -> colour, per theme.
#:
#: ``Mid`` is deliberately a readable muted *foreground* rather than the border
#: grey Qt intends, because that is what the app already uses it for: every
#: ``color: palette(mid)`` in the UI is a hint, a summary line or a help badge.
#: Borders take ``Dark`` and ``Shadow`` instead.
_THEMES: dict[str, dict[str, str]] = {
    # Chrome lighter than content, which is the way round every dark desktop
    # theme has it: the tree is a hole in the window rather than a panel on it,
    # so a full-height column reads as one surface instead of four.
    "dark": {
        "Window": "#262A31",
        "WindowText": "#D8DEE9",
        "Base": "#1E2127",
        "AlternateBase": "#23262D",
        "Text": "#D8DEE9",
        "Button": "#2F343D",
        "ButtonText": "#D8DEE9",
        "BrightText": "#FFFFFF",
        "PlaceholderText": "#6B7280",
        "ToolTipBase": "#2F343D",
        "ToolTipText": "#D8DEE9",
        "Highlight": ACCENT_DARK,
        "HighlightedText": "#FFFFFF",
        "Link": ACCENT_DARK,
        "LinkVisited": "#9B7FD4",
        "Mid": "#8A919E",
        "Midlight": "#363C46",
        "Dark": "#16181C",
        "Shadow": "#101216",
    },
    "light": {
        "Window": "#F4F5F7",
        "WindowText": "#1F2430",
        "Base": "#FFFFFF",
        "AlternateBase": "#EEF0F3",
        "Text": "#1F2430",
        "Button": "#E8EAED",
        "ButtonText": "#1F2430",
        "BrightText": "#000000",
        "PlaceholderText": "#8A909B",
        "ToolTipBase": "#FFFFFF",
        "ToolTipText": "#1F2430",
        "Highlight": ACCENT_LIGHT,
        "HighlightedText": "#FFFFFF",
        "Link": ACCENT_LIGHT,
        "LinkVisited": "#7A5CB8",
        "Mid": "#6B7280",
        "Midlight": "#F8F9FA",
        "Dark": "#C3C7CD",
        "Shadow": "#A8ADB5",
    },
}

#: The greyed-out group, set by hand rather than left to Fusion to derive.
#:
#: It is not decoration here: a list-view context row — the grey stand-in for a
#: parent that is not in this slice — is drawn with ``Disabled``/``WindowText``
#: (:func:`davpunk.ui.views.context_item`), and so are the uninteresting fields
#: in the merge dialog.  Fusion's own derivation blends toward the background
#: hard enough that on the dark palette those rows come out barely legible.
_DISABLED: dict[str, str] = {
    "dark": "#6B7280",
    "light": "#9AA0AA",
}

#: Density.  Qt's defaults are tuned for a general desktop app; this is a task
#: client that wants as many rows on screen as it can get.
#:
#: Padding only.  Giving ``QTreeView::item`` a *background* would take selection
#: and hover painting away from the style and hand it to the stylesheet, which
#: then has to redraw both by hand — and the board's drop states are drawn that
#: way already.  Padding leaves all of that where it is.
_COMPACT = """
QTreeView::item { padding: 1px 2px; }
QHeaderView::section {
    padding: 2px 6px;
    border: 0;
    border-bottom: 1px solid palette(dark);
    background: palette(window);
}
QToolTip {
    padding: 3px;
    border: 1px solid palette(dark);
    background: palette(base);
    color: palette(text);
}

/* Fusion outlines a check indicator with its own darkening of the *window*
   colour, which on a dark palette lands under the tree background: the box you
   tick a task off with comes out all but invisible.  Drawing the box here is
   the only way to give it a border that survives a dark theme.
   The cost is Fusion's checkmark, which a stylesheet cannot ask it to keep --
   a filled accent square stands in, and at this row height it reads further
   across a screen than the glyph did. */
QTreeView::indicator, QCheckBox::indicator, QMenu::indicator {
    width: 12px;
    height: 12px;
    border: 1px solid palette(mid);
    border-radius: 2px;
    background: palette(base);
}
QTreeView::indicator:checked, QCheckBox::indicator:checked, QMenu::indicator:checked {
    background: palette(highlight);
    border-color: palette(highlight);
}
QTreeView::indicator:disabled, QCheckBox::indicator:disabled {
    border-color: palette(dark);
}
"""


def palette_for(theme: str) -> QPalette:
    """The palette for ``theme``, falling back to dark for an unknown name."""
    colours = _THEMES.get(theme)
    if colours is None:
        # Deliberately not a config validation error: the config is read once at
        # startup and a bad one sends the user through the first-run wizard, and
        # a mistyped colour scheme is not worth that.
        log.warning("Unknown theme %r; using 'dark'", theme)
        colours = _THEMES["dark"]
        theme = "dark"

    palette = QPalette()
    for name, value in colours.items():
        palette.setColor(getattr(QPalette.ColorRole, name), QColor(value))

    greyed = QColor(_DISABLED[theme])
    disabled = QPalette.ColorGroup.Disabled
    for name in ("WindowText", "Text", "ButtonText", "PlaceholderText"):
        palette.setColor(disabled, getattr(QPalette.ColorRole, name), greyed)
    # A disabled selection that keeps the accent reads as still-selected; an
    # unfocused list should say "this was your row", not "this is live".
    palette.setColor(disabled, QPalette.ColorRole.Highlight, QColor(colours["Midlight"]))
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, greyed)
    return palette


def apply(app, theme: str) -> None:
    """Put ``theme`` on ``app``, under Fusion so the palette is actually used."""
    app.setStyle("Fusion")
    app.setPalette(palette_for(theme))
    app.setStyleSheet(_COMPACT)
