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
from davpunk.ui.viewmodel import checklist_progress

log = logging.getLogger("davpunk.ui.dialogs")

STATUSES = [s.value for s in Status]


class TaskEditor(QDialog):
    """Edit one task.  Read-only tasks show a banner and disable the fields."""

    def __init__(self, task: Task, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(task.summary or "New task")
        self.task = task
        self._original = task.model_copy(deep=True)

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
        if task.status:
            self.status.setCurrentText(task.status.value)
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

        form.addRow("Summary", self.summary)
        form.addRow("Description", self.description)
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
            for widget in (
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
            ):
                widget.setEnabled(False)
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)

    def changed_fields(self) -> dict[str, object]:
        """Only what actually changed — an update should not touch every column."""
        fields: dict[str, object] = {}
        if self.summary.text() != (self._original.summary or ""):
            fields["summary"] = self.summary.text() or None
        if self.description.toPlainText() != (self._original.description or ""):
            fields["description"] = self.description.toPlainText() or None
        if self.status.currentText() != (
            self._original.status.value if self._original.status else ""
        ):
            fields["status"] = Status(self.status.currentText())
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

    def __init__(self, calendars, current_calendar_id, has_children: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move to another list")

        layout = QVBoxLayout(self)
        self.target = QComboBox()
        for row in calendars:
            if row["id"] != current_calendar_id and row["available"]:
                self.target.addItem(row["display_name"] or row["href"], row["id"])
        layout.addWidget(QLabel("Move to:"))
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
