"""A filter bar: which lists, which tags, and a free-text box.

Eight calendars and a few hundred tasks make an unfiltered board unusable, so
the selection has to be multiple-choice on both axes — you rarely want exactly
one list, and "shopping *and* urgent" is a different question from "shopping
*or* urgent".

Selection is runtime state, not a stored preference, for the same reason
``show_completed`` is: it is a way of looking at the board right now.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QWidget,
)

from davpunk.ui.viewmodel import TaskFilter
from davpunk.ui.widgets import apply_help

log = logging.getLogger("davpunk.ui.filter_bar")


class _StayOpenMenu(QMenu):
    """A menu that a click does not dismiss.

    Qt closes a menu as soon as an action fires, checkable or not — fine for a
    command, wrong for a list of boxes: picking three lists would mean three
    trips to the button.  Every entry here is part of the same choice, so the
    menu stays put and closes only when the user says so, with Escape or by
    clicking away.
    """

    def mouseReleaseEvent(self, event) -> None:
        action = self.activeAction()
        if (
            action is not None
            and action.isEnabled()
            and self.actionGeometry(action).contains(event.position().toPoint())
        ):
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        """Space and Enter tick without leaving, so the keyboard behaves too."""
        keys = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
        action = self.activeAction()
        if event.key() in keys and action is not None and action.isEnabled():
            action.trigger()
            event.accept()
            return
        super().keyPressEvent(event)


class _CheckMenuButton(QToolButton):
    """A button whose menu is a list of checkable, independent entries.

    A ``QComboBox`` cannot express "three of eight selected", and checkable
    actions in a menu that stays open is the standard way to do it.
    """

    changed = Signal()

    def __init__(self, label: str, all_means_none: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        # Every task is in exactly one calendar, so ticking all of them really
        # is no restriction.  Tags are not like that: a task may carry none, so
        # "every tag" still excludes the untagged and must stay a filter.
        self._all_means_none = all_means_none
        self._menu = _StayOpenMenu(self)
        self._menu.setToolTipsVisible(True)
        self._menu.aboutToHide.connect(self._flush_pending)
        self.setMenu(self._menu)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setText(label)
        self._entries: dict[str, str] = {}
        self._pending: dict[str, str] | None = None

    def set_entries(self, entries: dict[str, str], selected: set[str]) -> None:
        """``{value: label}``, preserving whatever is still selectable.

        Rebuilding is skipped when nothing changed, and never happens while the
        menu is open: ``QMenu.clear()`` destroys the very ``QAction`` the user
        just ticked, and the refresh that follows a tick would otherwise delete
        it mid-signal.  Now that the menu stays open across a whole run of
        ticks, a genuine change arriving meanwhile is held and applied on close
        rather than waiting for whatever refresh happens to come next.
        """
        if entries == self._entries:
            self._pending = None
            return
        if self._menu.isVisible():
            self._pending = dict(entries)
            return

        self._pending = None
        self._entries = dict(entries)
        self._menu.clear()

        if not entries:
            empty = self._menu.addAction(f"(no {self._label.lower()} yet)")
            empty.setEnabled(False)
            self._update_text(set())
            return

        all_action = self._menu.addAction("Select all")
        none_action = self._menu.addAction("Clear")
        all_action.triggered.connect(lambda: self._set_all(True))
        none_action.triggered.connect(lambda: self._set_all(False))
        self._menu.addSeparator()

        for value, label in entries.items():
            action = self._menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value in selected)
            action.setData(value)
            action.toggled.connect(self._on_toggled)

        self._update_text(self.selected())

    def _flush_pending(self) -> None:
        """Apply a held update once the menu has closed.

        Deferred by a tick: ``aboutToHide`` runs while the actions are still
        alive and possibly still emitting, which is exactly when clearing them
        is unsafe.
        """
        if self._pending is None:
            return
        QTimer.singleShot(0, self._apply_pending)

    def _apply_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None or self._menu.isVisible():
            return
        # Keep whatever the user picked that still exists; entries that went
        # away drop out of the selection with them.
        before = self.selected()
        self.set_entries(pending, before & set(pending))
        if self.selected() != before:
            self.changed.emit()

    def _checkable_actions(self):
        return [a for a in self._menu.actions() if a.isCheckable()]

    def _set_all(self, checked: bool) -> None:
        for action in self._checkable_actions():
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)
        self._on_toggled()

    def _on_toggled(self, _checked: bool = False) -> None:
        self._update_text(self.selected())
        self.changed.emit()

    def _update_text(self, selected: set[str]) -> None:
        total = len(self._entries)
        # "All" rather than "8 of 8" — where selecting everything really is the
        # same as selecting nothing, saying so avoids implying a filter is on.
        if not selected or (len(selected) == total and self._all_means_none):
            self.setText(f"{self._label}: all")
        elif len(selected) == 1:
            only = next(iter(selected))
            self.setText(f"{self._label}: {self._entries.get(only, only)}")
        else:
            self.setText(f"{self._label}: {len(selected)} of {total}")

    def selected(self) -> set[str]:
        chosen = {a.data() for a in self._checkable_actions() if a.isChecked()}
        if self._all_means_none and len(chosen) == len(self._entries):
            # Everything selected is no restriction at all; normalise so the
            # filter does not have to care which of the two the user meant.
            return set()
        return chosen


class FilterBar(QWidget):
    """Lists · Tags · text, emitting :class:`TaskFilter` on every change."""

    filterChanged = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(6)

        self.calendars = _CheckMenuButton("Lists")
        apply_help(
            self.calendars,
            "Which task lists to show. Pick several, or none for all of them.",
        )
        self.tags = _CheckMenuButton("Tags", all_means_none=False)
        apply_help(
            self.tags,
            "Which tags to show. Only tags actually in use are offered; picking "
            "every one still hides tasks that carry no tag at all.",
        )

        self.match_all = QPushButton("any")
        self.match_all.setCheckable(True)
        self.match_all.setMaximumWidth(48)
        apply_help(
            self.match_all,
            "With several tags picked: 'any' shows a task carrying at least one "
            "of them, 'all' only one carrying every one.",
        )

        self.text = QLineEdit()
        self.text.setPlaceholderText("Filter by text…")
        self.text.setClearButtonEnabled(True)
        apply_help(self.text, "Substring match on the summary and description.")

        self.reset = QPushButton("Reset")
        apply_help(self.reset, "Clear every filter.")

        self.summary = QLabel("")
        self.summary.setStyleSheet("color: palette(mid);")

        layout.addWidget(self.calendars)
        layout.addWidget(self.tags)
        layout.addWidget(self.match_all)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.reset)
        layout.addWidget(self.summary)

        self._calendar_names: dict[str, str] = {}
        self.calendars.changed.connect(self._emit)
        self.tags.changed.connect(self._emit)
        self.text.textChanged.connect(self._emit)
        self.match_all.toggled.connect(self._on_match_all)
        self.reset.clicked.connect(self.clear)

    # ------------------------------------------------------------- population

    def set_calendars(self, calendars: dict[str, str]) -> None:
        self._calendar_names = dict(calendars)
        self.calendars.set_entries(calendars, self.calendars.selected())

    def set_tags(self, tags: list[str]) -> None:
        self.tags.set_entries({tag: tag for tag in tags}, self.tags.selected())

    # ----------------------------------------------------------------- state

    def current_filter(self) -> TaskFilter:
        return TaskFilter(
            calendar_ids=frozenset(self.calendars.selected()),
            tags=frozenset(self.tags.selected()),
            match_all_tags=self.match_all.isChecked(),
            text=self.text.text(),
        )

    def clear(self) -> None:
        for button in (self.calendars, self.tags):
            button._set_all(False)
        self.text.clear()
        self.match_all.setChecked(False)
        self._emit()

    def _on_match_all(self, checked: bool) -> None:
        self.match_all.setText("all" if checked else "any")
        self._emit()

    def _emit(self, *_args) -> None:
        current = self.current_filter()
        self.summary.setText(current.describe(self._calendar_names))
        self.filterChanged.emit(current)
