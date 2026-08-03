"""Rendering and editing one :class:`~davpunk.conflict.resolver.FieldGroup`.

The merge window shows every field twice as text and once as a live widget, so
both halves of that — how a value *reads* and how it is *typed* — live here
rather than in the dialog.  Parsing is deliberately the same shape as the task
editor's: an empty box means "unset", ``0`` priority means undefined, and a due
value that does not parse is refused rather than silently stored.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QWidget,
)

from davpunk.conflict.resolver import FieldGroup, FieldKind
from davpunk.models.task import Alarm, AlarmRelated, Status, sort_epoch

#: Shown where a value is absent, so "empty" never reads as "not loaded".
UNSET = "(unset)"
NO_STATUS = "(no status)"
NO_PRIORITY = "(undefined)"
NONE_LIST = "(none)"

STATUSES = [NO_STATUS, *(s.value for s in Status)]


class FieldError(ValueError):
    """What the user typed cannot become a value.  Blocks Accept."""


# ------------------------------------------------------------------ rendering


def render(group: FieldGroup, values: dict[str, Any]) -> str:
    """One line of text for a table cell."""
    value = values.get(group.primary)

    if group.kind is FieldKind.MULTILINE:
        return _render_multiline(value)
    if group.kind is FieldKind.STATUS:
        return str(value) if value else NO_STATUS
    if group.kind is FieldKind.PRIORITY:
        return str(value) if value else NO_PRIORITY
    if group.kind is FieldKind.PERCENT:
        return UNSET if value is None else f"{value} %"
    if group.kind is FieldKind.DATETIME:
        return _render_datetime(group, values)
    if group.kind is FieldKind.TAGS:
        return ", ".join(value) if value else NONE_LIST
    if group.kind is FieldKind.OPAQUE:
        return _render_opaque(group, value)
    return str(value) if value else UNSET


def render_full(group: FieldGroup, values: dict[str, Any]) -> str:
    """The whole value, for the detail pane under the table."""
    if group.kind is FieldKind.MULTILINE:
        return str(values.get(group.primary) or "")
    if group.kind is FieldKind.OPAQUE and group.primary == "alarms":
        alarms = values.get("alarms") or []
        return "\n".join(_render_one_alarm(a) for a in alarms) or NONE_LIST
    return render(group, values)


def _render_multiline(value: Any) -> str:
    """First line plus a line count.

    The count matters: two descriptions that share an opening line but differ
    three lines down would otherwise render identically in the table, which is
    the one thing a diff must never do.
    """
    if not value:
        return UNSET
    lines = str(value).splitlines()
    head = lines[0] if lines else ""
    return f"{head} … ({len(lines)} lines)" if len(lines) > 1 else head


def _render_datetime(group: FieldGroup, values: dict[str, Any]) -> str:
    value = values.get(group.fields[0])
    tzid = values.get(group.fields[1]) if len(group.fields) > 1 else None
    if not value:
        return UNSET
    return f"{value} · {tzid}" if tzid else str(value)


def _render_opaque(group: FieldGroup, value: Any) -> str:
    if group.primary != "alarms":
        return str(value) if value else NONE_LIST
    alarms = value or []
    if not alarms:
        return NONE_LIST
    if len(alarms) == 1:
        return _render_one_alarm(alarms[0])
    return f"{len(alarms)} alarms: " + "; ".join(_render_one_alarm(a) for a in alarms)


def _render_one_alarm(alarm: Alarm) -> str:
    anchor = "start" if alarm.related is AlarmRelated.START else "due"
    offset = alarm.trigger_offset
    if offset == 0:
        return f"at {anchor}"
    return f"{_duration(abs(offset))} {'before' if offset < 0 else 'after'} {anchor}"


def _duration(seconds: int) -> str:
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size} {unit}"
    return f"{seconds} s"


# -------------------------------------------------------------------- editing


class FieldEditor:
    """A widget bound to one group, plus the parsing that goes with it.

    ``on_change`` fires only for edits the *user* made: programmatic writes go
    through :meth:`set_values`, which mutes it, or the centre column would
    report itself hand-edited every time an arrow filled it in.
    """

    editable = True

    def __init__(self, group: FieldGroup, on_change: Callable[[], None] | None = None) -> None:
        self.group = group
        self._on_change = on_change
        self._muted = False
        self.widget = self._build()

    # -- subclass surface

    def _build(self) -> QWidget:  # pragma: no cover - abstract
        raise NotImplementedError

    def values(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def set_values(self, values: dict[str, Any]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- shared

    def _changed(self, *_args) -> None:
        if not self._muted and self._on_change is not None:
            self._on_change()

    def _quiet(self, write: Callable[[], None]) -> None:
        self._muted = True
        try:
            write()
        finally:
            self._muted = False


class _TextEditor(FieldEditor):
    def _build(self) -> QWidget:
        line = QLineEdit()
        line.textEdited.connect(self._changed)
        return line

    def values(self) -> dict[str, Any]:
        return {self.group.primary: self.widget.text().strip() or None}

    def set_values(self, values: dict[str, Any]) -> None:
        self._quiet(lambda: self.widget.setText(str(values.get(self.group.primary) or "")))


class _MultilineEditor(FieldEditor):
    def _build(self) -> QWidget:
        text = QPlainTextEdit()
        text.setTabChangesFocus(True)  # or Tab would type into a checklist
        text.textChanged.connect(self._changed)
        return text

    def values(self) -> dict[str, Any]:
        return {self.group.primary: self.widget.toPlainText() or None}

    def set_values(self, values: dict[str, Any]) -> None:
        self._quiet(lambda: self.widget.setPlainText(str(values.get(self.group.primary) or "")))


class _StatusEditor(FieldEditor):
    def _build(self) -> QWidget:
        combo = QComboBox()
        combo.addItems(STATUSES)
        combo.activated.connect(self._changed)  # activated: user only, not code
        return combo

    def values(self) -> dict[str, Any]:
        text = self.widget.currentText()
        return {self.group.primary: None if text == NO_STATUS else Status(text)}

    def set_values(self, values: dict[str, Any]) -> None:
        status = values.get(self.group.primary)
        self._quiet(lambda: self.widget.setCurrentText(str(status) if status else NO_STATUS))


class _SpinEditor(FieldEditor):
    """Priority and per-cent, which share a shape: a range plus one "unset"."""

    minimum = 0
    maximum = 100
    unset_at = 0
    unset_text = UNSET

    def _build(self) -> QWidget:
        spin = QSpinBox()
        spin.setRange(self.minimum, self.maximum)
        spin.setSpecialValueText(self.unset_text)
        spin.valueChanged.connect(self._changed)
        return spin

    def values(self) -> dict[str, Any]:
        value = self.widget.value()
        return {self.group.primary: None if value == self.unset_at else value}

    def set_values(self, values: dict[str, Any]) -> None:
        value = values.get(self.group.primary)
        self._quiet(lambda: self.widget.setValue(self.unset_at if value is None else int(value)))


class _PriorityEditor(_SpinEditor):
    #: PRIORITY 0 means "undefined" in RFC 5545, not "highest".
    minimum, maximum, unset_at, unset_text = 0, 9, 0, NO_PRIORITY


class _PercentEditor(_SpinEditor):
    #: 0 % is a real value here, so "unset" needs a slot of its own.
    minimum, maximum, unset_at, unset_text = -1, 100, -1, UNSET


class _DateTimeEditor(FieldEditor):
    """The value and its TZID, edited together because they mean nothing apart."""

    def _build(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self.value = QLineEdit()
        self.value.setPlaceholderText("20260731 or 20260731T170000")
        self.tzid = QLineEdit()
        self.tzid.setPlaceholderText("Europe/Berlin (blank = floating)")
        for line in (self.value, self.tzid):
            line.textEdited.connect(self._changed)
            layout.addWidget(line)
        return container

    def values(self) -> dict[str, Any]:
        value = self.value.text().strip() or None
        tzid = self.tzid.text().strip() or None
        if value is not None and sort_epoch(value, tzid) is None:
            raise FieldError(
                f"{self.group.label}: {value!r} is not a date or date-time "
                "(try 20260731 or 20260731T170000)"
            )
        return {self.group.fields[0]: value, self.group.fields[1]: tzid if value else None}

    def set_values(self, values: dict[str, Any]) -> None:
        def write() -> None:
            self.value.setText(str(values.get(self.group.fields[0]) or ""))
            self.tzid.setText(str(values.get(self.group.fields[1]) or ""))

        self._quiet(write)


class _TagsEditor(FieldEditor):
    def _build(self) -> QWidget:
        line = QLineEdit()
        line.setPlaceholderText("comma, separated")
        line.textEdited.connect(self._changed)
        return line

    def values(self) -> dict[str, Any]:
        tags = [part.strip() for part in self.widget.text().split(",")]
        return {self.group.primary: [tag for tag in tags if tag]}

    def set_values(self, values: dict[str, Any]) -> None:
        tags = values.get(self.group.primary) or []
        self._quiet(lambda: self.widget.setText(", ".join(tags)))


class _OpaqueEditor(FieldEditor):
    """Alarms and RRULE: transferable between the columns, never hand-typed.

    DavPunk never authors an RRULE, and an alarm list is not a text field — so
    the centre shows what it holds and the arrows are the only way to change it.
    """

    editable = False

    def _build(self) -> QWidget:
        self._values: dict[str, Any] = dict.fromkeys(self.group.fields)
        label = QLabel()
        label.setWordWrap(True)
        return label

    def values(self) -> dict[str, Any]:
        return dict(self._values)

    def set_values(self, values: dict[str, Any]) -> None:
        self._values = dict(values)
        self.widget.setText(render(self.group, values))


_EDITORS: dict[FieldKind, type[FieldEditor]] = {
    FieldKind.TEXT: _TextEditor,
    FieldKind.MULTILINE: _MultilineEditor,
    FieldKind.STATUS: _StatusEditor,
    FieldKind.PRIORITY: _PriorityEditor,
    FieldKind.PERCENT: _PercentEditor,
    FieldKind.DATETIME: _DateTimeEditor,
    FieldKind.TAGS: _TagsEditor,
    FieldKind.OPAQUE: _OpaqueEditor,
}


def editor_for(group: FieldGroup, on_change: Callable[[], None] | None = None) -> FieldEditor:
    return _EDITORS[group.kind](group, on_change)
