"""The Task model, its canonicalization invariant and its due semantics.

Two rules live here rather than in a caller, because they must hold on *every*
write path — UI edit, MCP ``set_status``, inbound server parse, conflict
resolution:

* :meth:`Task.canonicalized` — the STATUS / PERCENT-COMPLETE / COMPLETED
  invariant.
* :func:`due_deadline` — the single definition of "overdue".
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("davpunk.models.task")

#: Sparse spacing for X-DAVPUNK-ORDER; insertion takes the midpoint.
ORDER_STEP = 1000


class Status(StrEnum):
    NEEDS_ACTION = "NEEDS-ACTION"
    IN_PROCESS = "IN-PROCESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SyncState(StrEnum):
    NEW = "new"
    CLEAN = "clean"
    DIRTY = "dirty"
    CONFLICT = "conflict"
    PENDING_DELETE = "pending_delete"


class ReadOnlyReason(StrEnum):
    MULTIPART = "multipart"
    OVERSIZE = "oversize"
    CALENDAR_UNAVAILABLE = "calendar-unavailable"


class ChangeType(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


class AlarmRelated(StrEnum):
    START = "START"  # anchored to DTSTART
    END = "END"  # anchored to DUE


class Alarm(BaseModel):
    """A display VALARM with a relative trigger.  No REPEAT, no DURATION.

    ``trigger_offset`` is stored in **seconds**, so it is an *exact* duration in
    the RFC 5545 sense.  A ``-P1D`` TRIGGER (nominally "one day", which follows
    wall clock across a DST change) is normalised to -86400 s at parse and will
    therefore fire an hour off wall-clock on the two DST days a year.  That is
    the cost of the integer column, and it is the right trade: an exact offset
    is unambiguous, and DavPunk never rewrites a TRIGGER it did not author.
    """

    model_config = ConfigDict(frozen=False)

    related: AlarmRelated = AlarmRelated.END
    trigger_offset: int = 0  # seconds; negative = before the anchor

    def trigger_at(self, task: Task, tz: tzinfo | None = None) -> datetime | None:
        anchor = (
            dtstart_instant(task, tz)
            if self.related is AlarmRelated.START
            else due_instant(task, tz)
        )
        if anchor is None:
            return None
        # Absolute-seconds arithmetic: aware + timedelta with a ZoneInfo does
        # *wall-clock* arithmetic, which would silently reinterpret the offset.
        shifted = anchor.astimezone(UTC) + timedelta(seconds=self.trigger_offset)
        return shifted.astimezone(anchor.tzinfo)


class Task(BaseModel):
    """A VTODO as DavPunk understands it.

    ``*_value`` / ``*_tzid`` hold the original RFC 5545 value verbatim and are
    **authoritative for serialization**.  ``due`` / ``dtstart`` are coarse epoch
    sort keys: never serialized, and never used to decide overdue.
    """

    # validate_assignment: the parser and the sync engine both build a Task by
    # assigning fields one at a time, and the invariants this model exists to
    # enforce (PRIORITY 0 -> None, PERCENT-COMPLETE clamping) have to hold on
    # that path too, not only when a Task is constructed in one call.
    model_config = ConfigDict(validate_assignment=True)

    id: str | None = None  # local UUID; NEVER exposed over MCP
    calendar_id: str | None = None
    href: str | None = None
    uid: str = ""
    etag: str | None = None
    raw_ics: str | None = None
    read_only_reason: ReadOnlyReason | None = None

    summary: str | None = None
    description: str | None = None
    status: Status | None = None
    priority: int | None = None  # NULL = undefined; never 0
    percent_complete: int | None = None

    dtstart: int | None = None
    dtstart_value: str | None = None
    dtstart_tzid: str | None = None

    due: int | None = None
    due_value: str | None = None
    due_tzid: str | None = None

    completed: int | None = None  # always UTC per RFC; epoch is exact
    rrule: str | None = None  # opaque; not expanded in MVP
    url: str | None = None
    location: str | None = None

    last_modified: int | None = None
    created: int | None = None
    sequence: int = 0

    parent_uid: str | None = None
    davpunk_order: int | None = None
    kanban_col: str | None = None

    sync_state: SyncState = SyncState.CLEAN

    categories: list[str] = Field(default_factory=list)
    alarms: list[Alarm] = Field(default_factory=list)

    @field_validator("priority")
    @classmethod
    def _priority_zero_is_undefined(cls, value: int | None) -> int | None:
        """PRIORITY 0 means "undefined" in RFC 5545, not "highest"."""
        if value in (None, 0):
            return None
        if not 1 <= value <= 9:
            raise ValueError("PRIORITY must be 1-9, or absent")
        return value

    @field_validator("percent_complete")
    @classmethod
    def _percent_range(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(0, min(100, value))

    # ---------------------------------------------------------------- invariants

    def canonicalized(self) -> Task:
        """Apply the STATUS/PERCENT/COMPLETED invariant.  Returns a new Task.

        * COMPLETED       → COMPLETED timestamp set, PERCENT-COMPLETE forced 100
        * NEEDS-ACTION,
          IN-PROCESS      → COMPLETED cleared, PERCENT-COMPLETE left alone
        * CANCELLED       → COMPLETED cleared, PERCENT-COMPLETE *preserved*
        """
        out = self.model_copy(deep=True)
        fields = canonicalize_fields(
            {
                "status": out.status,
                "completed": out.completed,
                "percent_complete": out.percent_complete,
            }
        )
        out.status = fields["status"]
        out.completed = fields["completed"]
        out.percent_complete = fields["percent_complete"]
        return out

    # ------------------------------------------------------------------- dates

    def due_deadline(self, tz: tzinfo | None = None) -> datetime | None:
        return due_deadline(self, tz)

    def is_overdue(self, now: datetime | None = None, tz: tzinfo | None = None) -> bool:
        if self.status in (Status.COMPLETED, Status.CANCELLED):
            return False
        deadline = due_deadline(self, tz)
        if deadline is None:
            return False
        return (now or datetime.now(local_timezone())) > deadline

    # -------------------------------------------------------------- convenience

    @property
    def is_read_only(self) -> bool:
        return self.read_only_reason is not None

    @property
    def priority_band(self) -> str | None:
        """1-4 High, 5 Med, 6-9 Low, absent → None."""
        if self.priority is None:
            return None
        if self.priority <= 4:
            return "high"
        if self.priority == 5:
            return "medium"
        return "low"


def canonicalize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize a partial field mapping in place-safe fashion.

    Used by ``update_task_optimistic``, which is given a sparse dict rather than
    a whole Task.  Only touches ``completed`` / ``percent_complete`` when
    ``status`` is part of the update — a status-less edit must not clear a
    COMPLETED timestamp.
    """
    out = dict(fields)
    if "status" not in out:
        return out

    status = out["status"]
    if status is not None and not isinstance(status, Status):
        status = Status(str(status))
        out["status"] = status

    if status is Status.COMPLETED:
        out["completed"] = out.get("completed") or utc_now_epoch()
        out["percent_complete"] = 100
    else:
        # Every other explicitly-set status means not completed — including
        # clearing it outright, which is how a task goes back to the pool.
        out["completed"] = None
        # percent_complete: PRESERVED (user-confirmed decision)
    return out


def utc_now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def local_timezone() -> tzinfo:
    """The system local zone, as a tzinfo that arithmetic can use."""
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else UTC


_warned_tzids: set[str] = set()


def resolve_tzid(tzid: str | None) -> tzinfo | None:
    """Resolve a TZID, logging **once per TZID** when it is unknown."""
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if tzid not in _warned_tzids:
            _warned_tzids.add(tzid)
            log.warning("Unresolvable TZID %r; using UTC for sort keys only", tzid)
        return None


def _parse_value(value: str) -> date | datetime | None:
    """Parse an RFC 5545 DATE or DATE-TIME value.  Returns naive datetimes."""
    text = value.strip()
    if not text:
        return None
    if "T" not in text:
        try:
            return date.fromisoformat(text) if "-" in text else _date_from_basic(text)
        except ValueError:
            return None
    stem = text[:-1] if text.endswith("Z") else text
    for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(stem, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(stem)
    except ValueError:
        return None


def _date_from_basic(text: str) -> date:
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def _instant(value: str | None, tzid: str | None, tz: tzinfo | None) -> datetime | None:
    """The aware start-instant of a value, used for alarm anchors."""
    if not value:
        return None
    tz = tz or local_timezone()
    parsed = _parse_value(value)
    if parsed is None:
        return None
    if isinstance(parsed, datetime):
        if value.strip().endswith("Z"):
            return parsed.replace(tzinfo=UTC)
        zone = resolve_tzid(tzid)
        return parsed.replace(tzinfo=zone or tz)
    return datetime.combine(parsed, time.min, tzinfo=tz)


def due_instant(task: Task, tz: tzinfo | None = None) -> datetime | None:
    return _instant(task.due_value, task.due_tzid, tz)


def dtstart_instant(task: Task, tz: tzinfo | None = None) -> datetime | None:
    return _instant(task.dtstart_value, task.dtstart_tzid, tz)


def due_deadline(task: Task, tz: tzinfo | None = None) -> datetime | None:
    """The instant after which the task is overdue.

    A DATE-valued DUE is overdue only after the **end of that day, local time** —
    a task due ``20260731`` is overdue after 23:59:59.999999 local on the 31st,
    not from 00:00 UTC, which is what comparing against the coarse sort key
    would give.
    """
    tz = tz or local_timezone()
    if not task.due_value:
        return None
    return _deadline(task.due_value, task.due_tzid, tz)


def dtstart_deadline(task: Task, tz: tzinfo | None = None) -> datetime | None:
    """The same end-of-day rule, for "not started yet" filtering."""
    tz = tz or local_timezone()
    if not task.dtstart_value:
        return None
    return _deadline(task.dtstart_value, task.dtstart_tzid, tz)


def _deadline(value: str, tzid: str | None, tz: tzinfo) -> datetime | None:
    parsed = _parse_value(value)
    if parsed is None:
        return None
    if not isinstance(parsed, datetime):  # DATE → end of that day
        return datetime.combine(parsed, time.max, tzinfo=tz)
    if value.strip().endswith("Z"):
        return parsed.replace(tzinfo=UTC)
    zone = resolve_tzid(tzid)
    if zone is not None:
        return parsed.replace(tzinfo=zone)
    return parsed.replace(tzinfo=tz)  # floating → local, by definition


def sort_epoch(value: str | None, tzid: str | None) -> int | None:
    """The **coarse** epoch sort key: DATE → 00:00 UTC, floating → UTC.

    Never serialized, and never used for overdue decisions.
    """
    if not value:
        return None
    parsed = _parse_value(value)
    if parsed is None:
        return None
    if not isinstance(parsed, datetime):
        return int(datetime.combine(parsed, time.min, tzinfo=UTC).timestamp())
    if value.strip().endswith("Z"):
        return int(parsed.replace(tzinfo=UTC).timestamp())
    zone = resolve_tzid(tzid)
    return int(parsed.replace(tzinfo=zone or UTC).timestamp())


def local_day_bounds(day: date, tz: tzinfo | None = None) -> tuple[int, int]:
    """Epoch range ``[start, end)`` for a local day.

    SQL filters that need day-accurate boundaries pass a precomputed range from
    here rather than comparing against ``now()``.
    """
    tz = tz or local_timezone()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def next_local_midnight(now: datetime | None = None, tz: tzinfo | None = None) -> datetime:
    """When the UI must re-bucket Today / Overdue / Upcoming."""
    tz = tz or local_timezone()
    now = now or datetime.now(tz)
    return datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=tz)


def rebalance_orders(order_values: Iterable[int | None]) -> list[int]:
    """Assign sparse X-DAVPUNK-ORDER values to a run of siblings."""
    return [(i + 1) * ORDER_STEP for i, _ in enumerate(order_values)]
