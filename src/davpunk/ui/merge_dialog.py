"""The three-pane merge window: server on the left, mine on the right, the
result in the middle.  Mode A only.

The centre column is the **result**, not a common ancestor: DavPunk never stored
the version both sides started from, so this is a two-way merge presented in
three panes.  It starts out as whichever side is newer, and the user moves
values into it with the arrows or types over them.

Nothing is decided until *Accept*.  *Skip* leaves the conflict open and defers
it — the task stays in ``conflict`` and comes back at a later sync.
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from davpunk.conflict.resolver import (
    DIFF_GROUPS,
    ConflictView,
    MergeState,
    Resolution,
    Side,
    resolution_for,
)
from davpunk.models.task import Task
from davpunk.ui import fields
from davpunk.ui.keymap import Keymap

log = logging.getLogger("davpunk.ui.merge_dialog")

COLUMN_FIELD, COLUMN_REMOTE, COLUMN_TO_CENTRE, COLUMN_RESULT, COLUMN_FROM_LOCAL, COLUMN_LOCAL = (
    range(6)
)

ORIGIN_LABELS = {Side.REMOTE: "server", Side.LOCAL: "mine", None: "edited"}

#: The focused row's full value, under the table.  Fixed, so switching rows
#: does not move the table out from under the keyboard.
DETAIL_HEIGHT = 130


class MergeDialog(QDialog):
    """Mode A, field by field.

    ``resolution`` and ``merged`` are what the caller hands to
    :func:`~davpunk.conflict.resolver.resolve_conflict`; ``skipped`` says the
    user chose to decide later, which is *not* the same as having decided
    nothing — the caller defers the conflict rather than re-offering it.
    """

    def __init__(self, view: ConflictView, keymap: Keymap | None = None, parent=None) -> None:
        super().__init__(parent)
        self.view = view
        self.keymap = keymap or Keymap()
        self.state = MergeState(view)
        self.resolution: Resolution | None = None
        self.merged: Task | None = None
        self.skipped = False
        self._errors: dict[str, str] = {}

        self.setWindowTitle(f"Merge: {view.summary or view.task_id}")
        layout = QVBoxLayout(self)
        layout.addWidget(_wrapped(QLabel(self._headline())))

        self.table = self._build_table()
        layout.addWidget(self.table, 1)  # the table gets the spare height

        self.only_differences = QCheckBox("Only fields that differ")
        self.only_differences.toggled.connect(self._apply_filter)
        layout.addWidget(self.only_differences)

        layout.addWidget(self._build_detail())

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: palette(highlight); font-weight: bold;")
        layout.addWidget(self.error)

        layout.addWidget(self._build_buttons())

        for group in DIFF_GROUPS:
            self._sync_row(group.name)
        self.table.setCurrentCell(self._first_changed_row(), COLUMN_RESULT)
        self.table.setFocus()
        self.resize(940, 640)

    # ------------------------------------------------------------------ build

    def _headline(self) -> str:
        started = ORIGIN_LABELS[self.state.default_side]
        return (
            f"Both you and the server changed “{self.view.summary or self.view.task_id}”.\n"
            f"Server: {_when(self.view.remote)} · You: {_when(self.view.local)} — "
            f"the result starts from the newer one ({started}). "
            "Arrows move a value into the middle; the middle is what gets saved."
        )

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(len(DIFF_GROUPS), 6)
        table.setHorizontalHeaderLabels(["Field", "Server (remote)", "", "Result", "", "Mine"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.currentCellChanged.connect(lambda row, *_: self._show_detail(row))
        table.installEventFilter(self)

        for row, group in enumerate(DIFF_GROUPS):
            for column in (COLUMN_FIELD, COLUMN_REMOTE, COLUMN_RESULT, COLUMN_LOCAL):
                table.setItem(row, column, QTableWidgetItem(""))
            table.setCellWidget(
                row, COLUMN_TO_CENTRE, self._arrow("→", group.name, Side.REMOTE, "server")
            )
            table.setCellWidget(
                row, COLUMN_FROM_LOCAL, self._arrow("←", group.name, Side.LOCAL, "mine")
            )

        header = table.horizontalHeader()
        for column in (COLUMN_REMOTE, COLUMN_RESULT, COLUMN_LOCAL):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column in (COLUMN_FIELD, COLUMN_TO_CENTRE, COLUMN_FROM_LOCAL):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def _arrow(self, glyph: str, group: str, side: Side, side_name: str) -> QPushButton:
        button = QPushButton(glyph)
        button.setFlat(True)
        button.setToolTip(f"Take the {side_name} value into the result")
        # Without this the table loses focus on every click, and with it the
        # keyboard path that the same buttons exist to mirror.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda _checked=False: self.take(group, side))
        return button

    def _build_detail(self) -> QWidget:
        """The focused row, full length and in the same three columns.

        A table cell can only show the first line of a description, which is
        exactly where two versions tend to differ, so the row under it repeats
        the geometry with the whole value — and the middle one is where the
        typing happens.
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self.detail_label = QLabel()
        layout.addWidget(self.detail_label)

        self.editors = {}
        self.detail = QStackedWidget()
        for group in DIFF_GROUPS:
            editor = fields.editor_for(group, partial(self._edited, group.name))
            self.editors[group.name] = editor
            self.detail.addWidget(_top_aligned(editor))

        self.detail_remote = _read_only_pane()
        self.detail_local = _read_only_pane()
        panes = QWidget()
        row = QHBoxLayout(panes)
        row.setContentsMargins(0, 0, 0, 0)
        for widget, stretch in ((self.detail_remote, 1), (self.detail, 2), (self.detail_local, 1)):
            row.addWidget(widget, stretch)
        panes.setFixedHeight(DETAIL_HEIGHT)
        layout.addWidget(panes)
        return container

    def _build_buttons(self) -> QDialogButtonBox:
        buttons = QDialogButtonBox()
        take_server = buttons.addButton("Take all &server", QDialogButtonBox.ButtonRole.ResetRole)
        take_server.clicked.connect(lambda: self.take_all(Side.REMOTE))
        take_mine = buttons.addButton("Take all &mine", QDialogButtonBox.ButtonRole.ResetRole)
        take_mine.clicked.connect(lambda: self.take_all(Side.LOCAL))

        self.accept_button = buttons.addButton("&Accept", QDialogButtonBox.ButtonRole.AcceptRole)
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept)

        skip = buttons.addButton("S&kip for now", QDialogButtonBox.ButtonRole.RejectRole)
        skip.setToolTip("Leave the conflict open and come back to it at a later sync")
        skip.clicked.connect(self.reject)
        return buttons

    # ----------------------------------------------------------- transfers

    def take(self, group: str, side: Side) -> None:
        """Move one side's value into the result."""
        self.state.take(group, side)
        self._errors.pop(group, None)
        self.editors[group].set_values(self.state.values(group))
        self._sync_row(group)
        self._refresh_error()

    def take_all(self, side: Side) -> None:
        for group in DIFF_GROUPS:
            self.take(group.name, side)

    def _edited(self, group: str) -> None:
        """The user typed in the centre editor."""
        try:
            values = self.editors[group].values()
        except fields.FieldError as exc:
            self._errors[group] = str(exc)
            self._refresh_error()
            return

        self._errors.pop(group, None)
        self.state.edit(group, values)
        # Not the editor: writing the value back into the widget the user is
        # typing in would send the cursor to the end on every keystroke.
        self._sync_row(group, refresh_editor=False)
        self._refresh_error()

    # ------------------------------------------------------------- rendering

    def _cell(self, row: int, column: int) -> QTableWidgetItem:
        """The item at one grid position.

        The table is filled edge to edge when the dialog is built and rows are
        never inserted afterwards, so a null cell here is a bug in this dialog
        rather than a state the rendering has to cope with.
        """
        item = self.table.item(row, column)
        assert item is not None, f"merge table cell ({row}, {column}) was never populated"
        return item

    def _sync_row(self, group_name: str, *, refresh_editor: bool = True) -> None:
        group = next(g for g in DIFF_GROUPS if g.name == group_name)
        row = DIFF_GROUPS.index(group)
        diff = self.view.diff_for(group_name)
        origin = self.state.origin(group_name)
        interesting = diff.differs or origin is None

        marker = f"  ({ORIGIN_LABELS[origin]})" if interesting else ""
        field_item = self._cell(row, COLUMN_FIELD)
        field_item.setText(f"{group.label}{marker}")
        # Colour alone is a weak signal across themes, so the changed rows are
        # bold as well.
        font = field_item.font()
        font.setBold(interesting)
        field_item.setFont(font)
        self._cell(row, COLUMN_REMOTE).setText(fields.render(group, diff.values(Side.REMOTE)))
        self._cell(row, COLUMN_LOCAL).setText(fields.render(group, diff.values(Side.LOCAL)))
        self._cell(row, COLUMN_RESULT).setText(fields.render(group, self.state.values(group_name)))

        for column in (COLUMN_FIELD, COLUMN_REMOTE, COLUMN_RESULT, COLUMN_LOCAL):
            item = self._cell(row, column)
            item.setBackground(_changed_brush() if interesting else _plain_brush())
            item.setForeground(_text_brush(interesting))
            item.setToolTip(item.text())

        if refresh_editor:
            self.editors[group_name].set_values(self.state.values(group_name))
        if self.table.currentRow() == row:
            self._show_detail(row)

    def _show_detail(self, row: int) -> None:
        if not 0 <= row < len(DIFF_GROUPS):
            return
        group = DIFF_GROUPS[row]
        editor = self.editors[group.name]
        self.detail.setCurrentIndex(row)  # one page per group, in table order
        hint = "" if editor.editable else "  —  transfer only, not editable here"
        self.detail_label.setText(f"{group.label}{hint}")

        diff = self.view.diff_for(group.name)
        self.detail_remote.setPlainText(fields.render_full(group, diff.values(Side.REMOTE)))
        self.detail_local.setPlainText(fields.render_full(group, diff.values(Side.LOCAL)))

    def _apply_filter(self, only_differences: bool) -> None:
        for row, group in enumerate(DIFF_GROUPS):
            hidden = only_differences and not (
                self.view.diff_for(group.name).differs or self.state.origin(group.name) is None
            )
            self.table.setRowHidden(row, hidden)

    def _first_changed_row(self) -> int:
        for row, group in enumerate(DIFF_GROUPS):
            if self.view.diff_for(group.name).differs:
                return row
        return 0

    def _refresh_error(self) -> None:
        message = next(iter(self._errors.values()), "")
        self.error.setText(message)
        self.accept_button.setEnabled(not self._errors)

    # --------------------------------------------------------------- outcome

    def accept(self) -> None:
        if self._errors:
            self._refresh_error()
            QApplication.beep()
            return
        self.resolution, self.merged = resolution_for(self.state)
        super().accept()

    def reject(self) -> None:
        """Skip — including the window's close button, which means the same."""
        self.skipped = True
        super().reject()

    # -------------------------------------------------------------- keyboard

    def eventFilter(self, watched, event) -> bool:
        """Keys on the table.  Deliberately *not* on the dialog: the centre
        editors are text fields, and a dialog-wide ``l`` would be unable to
        type the letter.
        """
        if watched is not self.table or event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)

        row = self.table.currentRow()
        group = DIFF_GROUPS[row].name if 0 <= row < len(DIFF_GROUPS) else None
        key, text = event.key(), event.text()

        if group is not None:
            if key == Qt.Key.Key_Left or text == self.keymap["take_server"]:
                self.take(group, Side.REMOTE)
                return True
            if key == Qt.Key.Key_Right or text == self.keymap["take_local"]:
                self.take(group, Side.LOCAL)
                return True
        if text == self.keymap["take_all_server"]:
            self.take_all(Side.REMOTE)
            return True
        if text == self.keymap["take_all_local"]:
            self.take_all(Side.LOCAL)
            return True
        if text in ("j", "k"):
            self._step(1 if text == "j" else -1)
            return True
        return super().eventFilter(watched, event)

    def _step(self, delta: int) -> None:
        row = self.table.currentRow()
        while 0 <= row + delta < len(DIFF_GROUPS):
            row += delta
            if not self.table.isRowHidden(row):
                self.table.setCurrentCell(row, COLUMN_RESULT)
                return


# ------------------------------------------------------------------- helpers


def _wrapped(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    return label


def _top_aligned(editor: fields.FieldEditor) -> QWidget:
    """A one-line editor left floating in the middle of the pane reads as a
    layout accident, so everything but the text box sits at the top."""
    if isinstance(editor.widget, QPlainTextEdit):
        return editor.widget

    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(editor.widget)
    layout.addStretch(1)
    return page


def _read_only_pane() -> QPlainTextEdit:
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    pane.setStyleSheet("color: palette(mid);")
    return pane


def _when(task: Task | None) -> str:
    if task is None or task.last_modified is None:
        return "changed at an unknown time"
    return datetime.fromtimestamp(task.last_modified).astimezone().strftime("%Y-%m-%d %H:%M")


def _changed_brush():
    return QApplication.palette().brush(QPalette.ColorRole.AlternateBase)


def _plain_brush():
    return QApplication.palette().brush(QPalette.ColorRole.Base)


def _text_brush(interesting: bool):
    palette = QApplication.palette()
    group = QPalette.ColorGroup.Normal if interesting else QPalette.ColorGroup.Disabled
    return palette.brush(group, QPalette.ColorRole.WindowText)
