"""The task editor, the move dialog, the conflict dialog and the key overlay."""

from __future__ import annotations

import logging

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from davpunk.conflict.resolver import ConflictView, Mode, Resolution
from davpunk.models.task import Status, Task
from davpunk.ui.keymap import Keymap
from davpunk.ui.viewmodel import checklist_progress, descendants, parse_subtask_lines

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
        self._creating = creating
        self.calendar: QComboBox | None = None
        self.parent_task: QComboBox | None = None
        self.move_subtree: QCheckBox | None = None

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
            if not creating:
                # Changing this is a PUT to the new collection and a DELETE
                # from the old one rather than a column write, so the caller
                # sends it through the move path.  It is still the same
                # question the rest of this form asks — which list is this
                # task in — and sending the user to a second dialog to answer
                # it was the odd part.
                combo.setToolTip(
                    "Changing this moves the task: it is written to the new list "
                    "and removed from the old one."
                )
            self.calendar = combo

        # Only where a move can leave something behind.  Parent links resolve
        # within one calendar, so subtasks that stay put lose theirs — the same
        # question the move dialog asks, and for the same reason.
        if not creating and self.calendar is not None and self._has_children():
            checkbox = QCheckBox("Move subtasks too")
            checkbox.setChecked(True)
            checkbox.setToolTip(
                "Subtasks left behind become root tasks in the old list, "
                "because a parent link only resolves within one calendar."
            )
            # Dead until the list actually changes: a live checkbox that does
            # nothing is worse than one visibly not in play yet.
            checkbox.setEnabled(False)
            self.calendar.currentIndexChanged.connect(
                lambda _index: checkbox.setEnabled(self.moved_to() is not None)
            )
            self.move_subtree = checkbox

        if tasks is not None:
            self.parent_task = QComboBox()
            self._fill_parents()
            if self.calendar is not None:
                self.calendar.currentIndexChanged.connect(lambda _index: self._fill_parents())

        form.addRow("Summary", self.summary)
        form.addRow("Description", self.description)
        if self.calendar is not None:
            form.addRow("List", self.calendar)
        if self.move_subtree is not None:
            form.addRow("", self.move_subtree)
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
                *(w for w in (self.calendar, self.parent_task, self.move_subtree) if w is not None),
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

    def moved_to(self) -> str | None:
        """The list to move an existing task into, or ``None`` for staying put.

        Deliberately not part of :meth:`changed_fields`: the other fields are
        columns to write, and this one is a PUT to another collection and a
        DELETE from this one.  A caller that mistook it for a column would
        retarget the row without ever queueing the delete, and the task would
        end up in both lists on the server.
        """
        if self._creating:
            return None
        chosen = self.calendar_id()
        return chosen if chosen != self._original.calendar_id else None

    def wants_subtree(self) -> bool:
        """Whether a move takes the subtasks with it.  True when there are none
        to ask about, which is what the move dialog defaults to as well."""
        return self.move_subtree is None or self.move_subtree.isChecked()

    def _has_children(self) -> bool:
        return any(
            t.calendar_id == self.task.calendar_id and t.parent_uid == self.task.uid
            for t in self._tasks
        )

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


class BulkSubtaskDialog(QDialog):
    """Many subtasks under one parent: one line, one subtask.

    Deliberately not :class:`TaskEditor` with a repeat button.  The dialog that
    edits everything is the wrong shape for entering a list you already have in
    your head — twelve fields per item, twelve times — and the fields it would
    ask about are exactly the ones a freshly captured subtask has no answer for
    yet.  What comes out is a title and a place in the sibling order; the rest
    is what opening one of them afterwards is for.
    """

    def __init__(self, parent_task: Task, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add subtasks")
        # Big enough to see a list in.  A text box sized to one line of text
        # invites one line of text, which is the one shape this dialog is not
        # for.
        self.resize(460, 340)

        layout = QVBoxLayout(self)
        under = QLabel(f"Under “{parent_task.summary or parent_task.uid}”:")
        under.setWordWrap(True)
        layout.addWidget(under)

        self.lines = QPlainTextEdit()
        self.lines.setPlaceholderText(
            "One subtask per line.\n\nDraft the release notes\nTag the commit\nUpdate the flake"
        )
        layout.addWidget(self.lines)

        self.count = QLabel("")
        layout.addWidget(self.count)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._save = buttons.button(QDialogButtonBox.StandardButton.Save)

        # Return belongs to the text box here — it is how you get to the next
        # subtask — so the dialog needs its own way to say "done".
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(key), self).activated.connect(self._accept_if_any)

        self.lines.textChanged.connect(self._recount)
        self._recount()

    def summaries(self) -> list[str]:
        return parse_subtask_lines(self.lines.toPlainText())

    def _recount(self) -> None:
        """Say what Save will do before it does it.

        Blank lines and bullet markers are dropped on the way in, so the number
        of lines on screen and the number of subtasks about to exist are not
        always the same — and the count is the only place that difference is
        visible while it can still be corrected.
        """
        count = len(self.summaries())
        self.count.setText(
            "Nothing to add yet."
            if not count
            else f"Creates {count} subtask{'' if count == 1 else 's'}."
        )
        self._save.setEnabled(count > 0)

    def _accept_if_any(self) -> None:
        if self._save.isEnabled():
            self.accept()


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
    """Modes A′ and B: one side of the conflict does not exist.

    There is nothing to merge when a task is gone on one side — the honest
    question is which of two outcomes the user wants — so these keep a plain
    two-button form.  Mode A goes to
    :class:`~davpunk.ui.merge_dialog.MergeDialog` instead.

    ``skipped`` means "decide at a later sync", which the caller turns into a
    deferral rather than a re-offer.
    """

    def __init__(self, view: ConflictView, keymap: Keymap | None = None, parent=None) -> None:
        super().__init__(parent)
        self.view = view
        self.keymap = keymap or Keymap()
        self.resolution: Resolution | None = None
        self.merged: Task | None = None
        self.skipped = False

        self.setWindowTitle(f"Conflict: {view.summary or view.task_id}")
        layout = QVBoxLayout(self)
        headline = QLabel(_conflict_headline(view.mode))
        headline.setWordWrap(True)
        layout.addWidget(headline)
        layout.addWidget(_side_summary(view))

        buttons = QDialogButtonBox()
        for resolution in view.resolutions:
            button = buttons.addButton(
                _RESOLUTION_LABELS[resolution], QDialogButtonBox.ButtonRole.ActionRole
            )
            button.clicked.connect(lambda _checked=False, r=resolution: self._choose(r))
        skip = buttons.addButton("S&kip for now", QDialogButtonBox.ButtonRole.RejectRole)
        skip.setToolTip("Leave the conflict open and come back to it at a later sync")
        skip.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, resolution: Resolution) -> None:
        """No merged task: neither mode offers one.

        ``recreate`` keeps the row the user already has — the local snapshot is
        what it was built from, so re-writing it would change nothing.
        """
        self.resolution = resolution
        self.accept()

    def reject(self) -> None:
        """Skip — including the window's close button, which means the same."""
        self.skipped = True
        super().reject()


_RESOLUTION_LABELS = {
    Resolution.MERGE: "Merge && save",
    Resolution.RESTORE_SERVER: "Keep the server's version",
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


def _side_summary(view: ConflictView) -> QLabel:
    """What the surviving side actually says, so the choice is not blind."""
    task = view.remote if view.mode is Mode.LOCAL_DELETE else view.local
    which = "The server's version" if view.mode is Mode.LOCAL_DELETE else "Your version"
    lines = [f"{which}:"]
    if task is not None:
        lines.append(f"  {task.summary or '(no summary)'}")
        if task.status:
            lines.append(f"  status {task.status.value}")
        if task.due_value:
            lines.append(f"  due {task.due_value}")
    label = QLabel("\n".join(lines))
    label.setStyleSheet("color: palette(mid);")
    return label


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
