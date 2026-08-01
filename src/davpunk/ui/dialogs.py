"""The task editor, the move dialog, the conflict dialog and the key overlay."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from davpunk.conflict.resolver import (
    ConflictView,
    Mode,
    Resolution,
    merge_from_selection,
)
from davpunk.models.task import Status, Task
from davpunk.ui.keymap import Keymap
from davpunk.ui.viewmodel import checklist_progress, descendants

log = logging.getLogger("davpunk.ui.dialogs")

#: The editor's status choices.  "(no status)" is first and is a real value,
#: not a blank: a task nobody has picked up yet and one explicitly marked
#: NEEDS-ACTION are different things — that is the whole reason the board has a
#: "To Do" column beside "Needs Action" — and the editor has to be able to say
#: both, and to put a task back into the pool.
NO_STATUS = "(no status)"
STATUSES = [NO_STATUS, *(s.value for s in Status)]


class TaskEditor(QDialog):
    """Edit one task.  Read-only tasks show a banner and disable the fields.

    ``calendars`` and ``tasks`` are optional because the two pickers they feed
    can only be honest about choices they were given: without the calendar rows
    there is no list to pick from, and without the task list no parent.  A
    caller that passes neither gets exactly today's editor.
    """

    def __init__(
        self,
        task: Task,
        parent=None,
        *,
        calendars=None,
        tasks: list[Task] | None = None,
        creating: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(task.summary or "New task")
        self.task = task
        self._original = task.model_copy(deep=True)
        self._tasks = list(tasks) if tasks is not None else []
        self.calendar: QComboBox | None = None
        self.parent_task: QComboBox | None = None

        layout = QVBoxLayout(self)
        banner = _banner_for(task)
        if banner:
            label = QLabel(banner)
            label.setWordWrap(True)
            label.setObjectName("readOnlyBanner")
            layout.addWidget(label)

        form = QFormLayout()
        self.summary = QLineEdit(task.summary or "")
        self.description = QPlainTextEdit(task.description or "")
        self.status = QComboBox()
        self.status.addItems(STATUSES)
        self.status.setCurrentText(task.status.value if task.status else NO_STATUS)
        self.priority = QSpinBox()
        self.priority.setRange(0, 9)
        self.priority.setSpecialValueText("(undefined)")  # 0 means undefined
        self.priority.setValue(task.priority or 0)
        self.percent = QSpinBox()
        self.percent.setRange(0, 100)
        self.percent.setValue(task.percent_complete or 0)
        self.due = QLineEdit(task.due_value or "")
        self.due.setPlaceholderText("20260731 or 20260731T170000")
        self.due_tzid = QLineEdit(task.due_tzid or "")
        self.due_tzid.setPlaceholderText("Europe/Berlin (blank = floating)")
        self.categories = QLineEdit(", ".join(task.categories))
        self.location = QLineEdit(task.location or "")
        self.url = QLineEdit(task.url or "")

        if calendars is not None:
            combo = QComboBox()
            for row in calendars:
                if row["available"] or row["id"] == task.calendar_id:
                    combo.addItem(row["display_name"] or row["href"], row["id"])
            index = combo.findData(task.calendar_id)
            if index >= 0:
                combo.setCurrentIndex(index)
            # Changing the list of an existing task is a PUT to the new
            # collection and a DELETE from the old one, not a column write, so
            # it belongs to the move dialog and its subtree question.
            combo.setEnabled(creating)
            if not creating:
                combo.setToolTip("Use “Move to another list” to change this.")
            self.calendar = combo

        if tasks is not None:
            self.parent_task = QComboBox()
            self._fill_parents()
            if self.calendar is not None:
                self.calendar.currentIndexChanged.connect(lambda _index: self._fill_parents())

        form.addRow("Summary", self.summary)
        form.addRow("Description", self.description)
        if self.calendar is not None:
            form.addRow("List", self.calendar)
        if self.parent_task is not None:
            form.addRow("Parent", self.parent_task)
        form.addRow("Status", self.status)
        form.addRow("Priority", self.priority)
        form.addRow("% complete", self.percent)
        form.addRow("Due", self.due)
        form.addRow("Due TZID", self.due_tzid)
        form.addRow("Tags", self.categories)
        form.addRow("Location", self.location)
        form.addRow("URL", self.url)
        layout.addLayout(form)

        done, total = checklist_progress(task.description)
        if total:
            layout.addWidget(QLabel(f"Checklist: {done}/{total} done"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if task.is_read_only or task.sync_state.value == "conflict":
            editable = [
                self.summary,
                self.description,
                self.status,
                self.priority,
                self.percent,
                self.due,
                self.due_tzid,
                self.categories,
                self.location,
                self.url,
                *(w for w in (self.calendar, self.parent_task) if w is not None),
            ]
            for widget in editable:
                widget.setEnabled(False)
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)

    def _fill_parents(self) -> None:
        """Offer only tasks in the selected list, and never a cycle.

        ``RELATED-TO`` resolves within one calendar, so a parent from another
        one would be a link that never renders; and the task itself or one of
        its own descendants would be a cycle the tree walker then has to break.
        """
        assert self.parent_task is not None
        wanted = (
            self.parent_task.currentData() if self.parent_task.count() else self.task.parent_uid
        )
        calendar_id = self.calendar_id()
        forbidden = descendants(self.task, self._tasks) | {self.task.uid}

        self.parent_task.blockSignals(True)
        self.parent_task.clear()
        self.parent_task.addItem("(no parent)", None)
        for candidate in sorted(
            (t for t in self._tasks if t.calendar_id == calendar_id and t.uid not in forbidden),
            key=lambda t: ((t.summary or "").casefold(), t.uid),
        ):
            self.parent_task.addItem(candidate.summary or candidate.uid, candidate.uid)

        index = self.parent_task.findData(wanted)
        self.parent_task.setCurrentIndex(max(index, 0))
        self.parent_task.blockSignals(False)

    def status_value(self) -> Status | None:
        """The chosen status, with ``None`` for the pool."""
        text = self.status.currentText()
        return Status(text) if text != NO_STATUS else None

    def calendar_id(self) -> str | None:
        """Which list the task belongs to — the picker's, or the task's own."""
        if self.calendar is None:
            return self.task.calendar_id
        return self.calendar.currentData()

    def changed_fields(self) -> dict[str, object]:
        """Only what actually changed — an update should not touch every column."""
        fields: dict[str, object] = {}
        if self.summary.text() != (self._original.summary or ""):
            fields["summary"] = self.summary.text() or None
        if self.description.toPlainText() != (self._original.description or ""):
            fields["description"] = self.description.toPlainText() or None
        if self.status_value() != self._original.status:
            fields["status"] = self.status_value()
        if self.priority.value() != (self._original.priority or 0):
            fields["priority"] = self.priority.value() or None
        if self.percent.value() != (self._original.percent_complete or 0):
            fields["percent_complete"] = self.percent.value()
        if self.due.text() != (self._original.due_value or ""):
            fields["due_value"] = self.due.text() or None
        if self.due_tzid.text() != (self._original.due_tzid or ""):
            fields["due_tzid"] = self.due_tzid.text() or None
        if self.location.text() != (self._original.location or ""):
            fields["location"] = self.location.text() or None
        if self.url.text() != (self._original.url or ""):
            fields["url"] = self.url.text() or None

        if self.parent_task is not None:
            chosen = self.parent_task.currentData()
            if chosen != self._original.parent_uid:
                # The caller routes this through reparent_fields, so the task
                # also lands at the end of its new siblings rather than keeping
                # an order value that belongs to the group it left.
                fields["parent_uid"] = chosen

        tags = [t.strip() for t in self.categories.text().split(",") if t.strip()]
        if tags != self._original.categories:
            fields["categories"] = tags
        return fields


def _banner_for(task: Task) -> str:
    if task.sync_state.value == "conflict":
        return "⚠ This task has an unresolved conflict. Edits are rejected until you resolve it."
    if task.read_only_reason is None:
        return ""
    return {
        "multipart": (
            "⚠ Read-only: this resource holds a recurring series with overrides. "
            "DavPunk will not rewrite it, because doing so would destroy them. "
            "You can still delete or move it."
        ),
        "oversize": (
            "⚠ Read-only: this resource is larger than max_resource_bytes. "
            "You can still delete or move it."
        ),
        "calendar-unavailable": (
            "⚠ Read-only: this task's collection is not answering. It will "
            "become editable again when the collection returns."
        ),
    }.get(task.read_only_reason.value, "⚠ Read-only.")


class MoveDialog(QDialog):
    """Move to another list, with the subtree prompt."""

    def __init__(
        self,
        calendars,
        current_calendar_id,
        has_children: bool,
        parent=None,
        *,
        fixed_target: str | None = None,
        heading: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move to another list")

        layout = QVBoxLayout(self)
        self.target = QComboBox()
        for row in calendars:
            if fixed_target is not None:
                if row["id"] == fixed_target:
                    self.target.addItem(row["display_name"] or row["href"], row["id"])
            elif row["id"] != current_calendar_id and row["available"]:
                self.target.addItem(row["display_name"] or row["href"], row["id"])
        if fixed_target is not None:
            # A paste already named the destination by where it was pasted; the
            # combo is here to show it, not to reopen the question.
            self.target.setEnabled(False)
        layout.addWidget(QLabel(heading or "Move to:"))
        layout.addWidget(self.target)

        self.move_subtree = QCheckBox("Move subtasks too")
        self.move_subtree.setChecked(True)
        if has_children:
            # Parent resolution is per-calendar, so leaving children behind
            # orphans them; declining promotes them to root in the source.
            self.move_subtree.setToolTip(
                "Subtasks left behind become root tasks in the source list, "
                "because a parent link only resolves within one calendar."
            )
            layout.addWidget(self.move_subtree)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def target_calendar_id(self) -> str | None:
        return self.target.currentData()

    def wants_subtree(self) -> bool:
        return self.move_subtree.isChecked()


class ConflictDialog(QDialog):
    """Modes A / A′ / B, built from the resolver's view."""

    def __init__(self, view: ConflictView, keymap: Keymap | None = None, parent=None) -> None:
        super().__init__(parent)
        self.view = view
        self.keymap = keymap or Keymap()
        self.resolution: Resolution | None = None
        self.merged: Task | None = None
        self._choices: dict[str, QButtonGroup] = {}

        self.setWindowTitle(f"Conflict: {view.summary or view.task_id}")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(_conflict_headline(view.mode)))

        if view.mode is Mode.BOTH_CHANGED:
            layout.addWidget(self._build_table())

        buttons = QDialogButtonBox()
        for resolution in view.resolutions:
            button = buttons.addButton(
                _RESOLUTION_LABELS[resolution], QDialogButtonBox.ButtonRole.ActionRole
            )
            button.clicked.connect(lambda _checked=False, r=resolution: self._choose(r))
        if view.mode is Mode.BOTH_CHANGED:
            merge = buttons.addButton("Merge && save", QDialogButtonBox.ButtonRole.AcceptRole)
            merge.clicked.connect(lambda: self._choose(Resolution.MERGE))
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _build_table(self) -> QTableWidget:
        changed = self.view.changed
        table = QTableWidget(len(changed), 4)
        table.setHorizontalHeaderLabels(["Field", "Local value", "Server value", "Use"])
        table.verticalHeader().setVisible(False)

        for row, diff in enumerate(changed):
            table.setItem(row, 0, QTableWidgetItem(diff.field))
            table.setItem(row, 1, QTableWidgetItem(_render(diff.local)))
            table.setItem(row, 2, QTableWidgetItem(_render(diff.remote)))

            cell = QHBoxLayout()
            local = QRadioButton("L")
            server = QRadioButton("S")
            local.setChecked(True)
            group = QButtonGroup(self)
            group.addButton(local, 0)
            group.addButton(server, 1)
            self._choices[diff.field] = group
            cell.addWidget(local)
            cell.addWidget(server)
            container = QLabel()
            container.setLayout(cell)
            table.setCellWidget(row, 3, container)

        table.resizeColumnsToContents()
        return table

    def take_all(self, server: bool) -> None:
        for group in self._choices.values():
            group.button(1 if server else 0).setChecked(True)

    def selected_remote_fields(self) -> set[str]:
        return {field for field, group in self._choices.items() if group.checkedId() == 1}

    def _choose(self, resolution: Resolution) -> None:
        self.resolution = resolution
        if resolution is Resolution.MERGE:
            self.merged = merge_from_selection(self.view, self.selected_remote_fields())
        self.accept()

    def keyPressEvent(self, event) -> None:
        """``l``/``s`` per field, ``a``/``A`` for all, Enter to save."""
        text = event.text()
        if text == self.keymap["take_all_local"]:
            self.take_all(server=False)
            return
        if text == self.keymap["take_all_server"]:
            self.take_all(server=True)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.view.mode is (
            Mode.BOTH_CHANGED
        ):
            self._choose(Resolution.MERGE)
            return
        super().keyPressEvent(event)


_RESOLUTION_LABELS = {
    Resolution.MERGE: "Merge && save",
    Resolution.RESTORE_SERVER: "Take all server",
    Resolution.DELETE_ANYWAY: "Delete anyway",
    Resolution.RECREATE: "Recreate on server",
    Resolution.ACCEPT_DELETION: "Accept deletion",
}


def _conflict_headline(mode: Mode) -> str:
    return {
        Mode.BOTH_CHANGED: "Both you and the server changed this task.",
        Mode.LOCAL_DELETE: (
            "The server has a newer version of a task you deleted. "
            "Delete it anyway, or keep the server's version?"
        ),
        Mode.SERVER_DELETED: ("The server no longer has this task, but you changed it locally."),
    }[mode]


def _render(value) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"
    return str(value)


class KeymapOverlay(QDialog):
    """The ``?`` overlay."""

    def __init__(self, keymap: Keymap, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Key bindings")
        layout = QVBoxLayout(self)
        text = QPlainTextEdit(keymap.overlay_text())
        text.setReadOnly(True)
        text.setMinimumSize(520, 560)
        layout.addWidget(text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
