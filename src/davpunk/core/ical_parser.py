"""RFC 5545 parsing and — more importantly — *serialization by mutation*.

> DavPunk preserves the **entire VCALENDAR component tree**.  Every
> calendar-level property (``PRODID``, ``VERSION``, ``CALSCALE``, ``METHOD``,
> ``X-WR-*``) and every sibling component (``VTIMEZONE``, additional ``VTODO``s)
> is written back untouched.  Only the owned properties inside the single target
> VTODO are mutated.  DavPunk never rebuilds a resource from scratch.

Scoping that at the VTODO level would permit an implementation that rebuilds the
wrapper and drops the ``VTIMEZONE`` definitions, breaking every ``TZID=``
reference for every other client using the same collection.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, cast

from icalendar import Calendar as ICalendar
from icalendar import Timezone as ITimezone
from icalendar import Todo as ITodo
from icalendar.prop import vText

from davpunk.models.task import (
    Alarm,
    AlarmRelated,
    ReadOnlyReason,
    Status,
    Task,
    sort_epoch,
)

log = logging.getLogger("davpunk.core.ical_parser")

PRODID = "-//DavPunk//DavPunk 0.1//EN"

#: Properties DavPunk owns inside the target VTODO.  Everything else survives
#: byte-identical.  RRULE is read-only pass-through.
OWNED_PROPERTIES = frozenset(
    {
        "SUMMARY",
        "DESCRIPTION",
        "STATUS",
        "PRIORITY",
        "PERCENT-COMPLETE",
        "CATEGORIES",
        "DTSTART",
        "DUE",
        "COMPLETED",
        "RELATED-TO",
        "LAST-MODIFIED",
        "SEQUENCE",
        "X-DAVPUNK-ORDER",
        "X-DAVPUNK-KANBAN-COL",
        "URL",
        "LOCATION",
    }
)

X_ORDER = "X-DAVPUNK-ORDER"
X_KANBAN = "X-DAVPUNK-KANBAN-COL"

DEFAULT_MAX_RESOURCE_BYTES = 262_144


class ICalParseError(Exception):
    """The bytes are not a usable VCALENDAR."""


# ------------------------------------------------------------------- parsing


def parse_resource(
    raw_ics: str | bytes,
    *,
    max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
) -> Task:
    """Parse one CalDAV resource into a :class:`Task`.

    ``task.raw_ics`` is always the **complete** VCALENDAR, whatever the
    quarantine outcome.
    """
    text = raw_ics.decode("utf-8") if isinstance(raw_ics, bytes) else raw_ics
    try:
        calendar = ICalendar.from_ical(text)
    except Exception as exc:  # icalendar raises a wide range
        raise ICalParseError(str(exc)) from exc

    todos = [cast(ITodo, c) for c in calendar.walk("VTODO")]
    if not todos:
        raise ICalParseError("resource contains no VTODO")

    read_only: ReadOnlyReason | None = None
    if len(todos) > 1:
        # A recurring series with RECURRENCE-ID overrides lives in one resource
        # as several VTODOs.  Re-serializing the master would destroy them, so
        # the whole resource is quarantined read-only.
        read_only = ReadOnlyReason.MULTIPART

    master = next((t for t in todos if "RECURRENCE-ID" not in t), todos[0])
    task = _task_from_todo(master)

    if len(text.encode("utf-8")) > max_resource_bytes:
        read_only = read_only or ReadOnlyReason.OVERSIZE

    task.read_only_reason = read_only
    task.raw_ics = text
    return task


def _task_from_todo(todo: ITodo) -> Task:
    task = Task(uid=_text(todo, "UID") or "")

    task.summary = _text(todo, "SUMMARY")
    task.description = _text(todo, "DESCRIPTION")
    task.location = _text(todo, "LOCATION")
    task.url = _text(todo, "URL")

    status = _text(todo, "STATUS")
    if status:
        try:
            task.status = Status(status.upper())
        except ValueError:
            log.debug("Unknown STATUS %r; leaving unset", status)

    task.priority = _int(todo, "PRIORITY")  # 0 -> None via the model validator
    task.percent_complete = _int(todo, "PERCENT-COMPLETE")
    task.sequence = _int(todo, "SEQUENCE") or 0

    task.dtstart_value, task.dtstart_tzid = _date_value(todo, "DTSTART")
    task.due_value, task.due_tzid = _date_value(todo, "DUE")
    task.dtstart = sort_epoch(task.dtstart_value, task.dtstart_tzid)
    task.due = sort_epoch(task.due_value, task.due_tzid)

    completed_value, completed_tzid = _date_value(todo, "COMPLETED")
    task.completed = sort_epoch(completed_value, completed_tzid)
    task.created = sort_epoch(*_date_value(todo, "CREATED"))
    task.last_modified = sort_epoch(*_date_value(todo, "LAST-MODIFIED"))

    rrule = todo.get("RRULE")
    if rrule is not None:  # opaque pass-through; never expanded in MVP
        task.rrule = rrule.to_ical().decode("utf-8", "replace")

    task.categories = _categories(todo)
    task.parent_uid = _parent_uid(todo)
    task.davpunk_order = _int(todo, X_ORDER)
    task.kanban_col = _text(todo, X_KANBAN)
    task.alarms = _alarms(todo, task)
    return task


def _text(component: Any, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0]
    text = str(value)
    return text if text != "" else None


def _int(component: Any, name: str) -> int | None:
    raw = _text(component, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        log.debug("Non-integer %s value %r; ignoring", name, raw)
        return None


def _date_value(component: Any, name: str) -> tuple[str | None, str | None]:
    """The **original** RFC 5545 value and TZID, verbatim.

    The stored value is what gets serialized back; the epoch columns derived
    from it are coarse sort keys and are never written to the wire.
    """
    prop = component.get(name)
    if prop is None:
        return None, None

    tzid = None
    params = getattr(prop, "params", None)
    if params:
        tzid = params.get("TZID")
        if tzid is not None:
            tzid = str(tzid)

    raw = prop.to_ical()
    value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    return value, tzid


def _categories(todo: ITodo) -> list[str]:
    prop = todo.get("CATEGORIES")
    if prop is None:
        return []
    out: list[str] = []
    for entry in prop if isinstance(prop, list) else [prop]:
        cats = getattr(entry, "cats", None)
        if cats:
            out.extend(str(c) for c in cats)
        else:
            out.extend(part for part in str(entry).split(",") if part)
    return [c.strip() for c in out if c.strip()]


def _parent_uid(todo: ITodo) -> str | None:
    """Only the **first** ``RELATED-TO;RELTYPE=PARENT`` is honored."""
    prop = todo.get("RELATED-TO")
    if prop is None:
        return None
    for entry in prop if isinstance(prop, list) else [prop]:
        reltype = str(getattr(entry, "params", {}).get("RELTYPE", "PARENT")).upper()
        if reltype == "PARENT":
            return str(entry)
    return None


_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration_seconds(value: str) -> int | None:
    match = _DURATION_RE.match(value.strip().upper())
    if not match or match.group(0) in ("P", "-P", "+P"):
        return None
    parts = match.groupdict()
    total = (
        int(parts["weeks"] or 0) * 604800
        + int(parts["days"] or 0) * 86400
        + int(parts["hours"] or 0) * 3600
        + int(parts["minutes"] or 0) * 60
        + int(parts["seconds"] or 0)
    )
    return -total if parts["sign"] == "-" else total


def _alarms(todo: ITodo, task: Task) -> list[Alarm]:
    """Display alarms with a **relative** trigger and a resolvable anchor.

    A relative trigger with no anchor is invalid and is dropped at parse; the
    alarm editor is disabled for such a task.  REPEAT/DURATION and the EMAIL
    action are out of scope.
    """
    out: list[Alarm] = []
    for alarm in todo.walk("VALARM"):
        action = _text(alarm, "ACTION")
        if action and action.upper() != "DISPLAY":
            continue

        trigger = alarm.get("TRIGGER")
        if trigger is None:
            continue

        params = getattr(trigger, "params", {})
        if str(params.get("VALUE", "DURATION")).upper() == "DATE-TIME":
            continue  # absolute trigger: out of scope

        related = AlarmRelated.END
        if str(params.get("RELATED", "START")).upper() == "START":
            related = AlarmRelated.START

        offset = _trigger_seconds(trigger)
        if offset is None:
            continue

        anchor = task.dtstart_value if related is AlarmRelated.START else task.due_value
        if not anchor:
            log.debug("Dropping VALARM on %s: no %s anchor", task.uid[:8], related.value)
            continue

        out.append(Alarm(related=related, trigger_offset=offset))
    return out


def _trigger_seconds(trigger: Any) -> int | None:
    value = getattr(trigger, "dt", None)
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    raw = trigger.to_ical()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    return parse_duration_seconds(text)


# ------------------------------------------------------------- serialization


def serialize_task(task: Task, *, rewrite_alarms: bool = False) -> str:
    """Write ``task`` back into its own ``raw_ics``, mutating in place.

    Only :data:`OWNED_PROPERTIES` inside the target VTODO change.  Every other
    property, every calendar-level property and every sibling component —
    including ``VTIMEZONE`` — is preserved **byte for byte**, because they are
    never re-rendered: the modified VTODO block is spliced back into the
    original text.

    Re-rendering the whole VCALENDAR is not good enough even with
    ``sorted=False``.  ``icalendar`` canonicalises structured values on the way
    out — it rewrites ``RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU`` as
    ``FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3`` — so a VTIMEZONE nobody touched would
    still come back textually different from what the server holds.
    """
    if task.read_only_reason is not None:
        raise ICalParseError(f"refusing to re-serialize a {task.read_only_reason.value} resource")

    if not task.raw_ics:
        calendar = _new_calendar()
        todo = ITodo()
        calendar.add_component(todo)
        _apply_owned_properties(todo, task)
        if rewrite_alarms or task.alarms:
            _rewrite_alarms(todo, task)
        _ensure_vtimezones(calendar, task)
        return calendar.to_ical(sorted=False).decode("utf-8")

    calendar = ICalendar.from_ical(task.raw_ics)
    todos = [cast(ITodo, c) for c in calendar.walk("VTODO")]
    if len(todos) != 1:
        raise ICalParseError("expected exactly one VTODO in an editable resource")
    todo = todos[0]

    original_order = list(todo.keys())
    _apply_owned_properties(todo, task)
    _restore_order(todo, original_order)
    if rewrite_alarms:
        _rewrite_alarms(todo, task)

    # sorted=False inside the VTODO too: an untouched property there round-trips
    # byte for byte, in its original position.
    rendered = todo.to_ical(sorted=False).decode("utf-8")
    extra = _missing_vtimezones(calendar, task)
    return _splice_vtodo(task.raw_ics, rendered, extra)


def _splice_vtodo(original: str, vtodo_text: str, extra_components: list[str]) -> str:
    """Replace the single VTODO block in ``original``, leaving all other bytes.

    An editable resource has exactly one VTODO — a resource with more is
    quarantined ``multipart`` and never reaches here — so the block is
    unambiguous.
    """
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.split(newline)

    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip().upper()
        if stripped == "BEGIN:VTODO" and start is None:
            start = index
        elif stripped == "END:VTODO" and start is not None:
            end = index
            break
    if start is None or end is None:
        raise ICalParseError("could not locate the VTODO block to replace")

    body = vtodo_text.rstrip("\r\n").split("\r\n")
    injected: list[str] = []
    for component in extra_components:
        injected.extend(component.rstrip("\r\n").split("\r\n"))

    merged = lines[:start] + injected + body + lines[end + 1 :]
    return "\r\n".join(merged)


def _restore_order(component: Any, original_keys: list[str]) -> None:
    """Put properties back where they were; genuinely new ones go last.

    Replacing a property is ``del`` + ``add``, which would otherwise move it to
    the end of the VTODO and churn the diff other clients see.
    """
    values = {key: component[key] for key in component}
    kept = [key for key in original_keys if key in values]
    added = [key for key in values if key not in original_keys]
    for key in list(component):
        del component[key]
    for key in kept + added:
        component[key] = values[key]


def _new_calendar() -> ICalendar:
    """A calendar DavPunk itself creates — the only place PRODID is ours."""
    calendar = ICalendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    calendar.add("CALSCALE", "GREGORIAN")
    return calendar


def _set(todo: ITodo, name: str, value: Any) -> None:
    if name in todo:
        del todo[name]
    if value is not None:
        todo.add(name, value)


def _set_raw(todo: ITodo, name: str, value: str | None, tzid: str | None = None) -> None:
    """Write a date/time property back **verbatim**, TZID and all.

    ``icalendar`` would happily re-render a value it had parsed into a
    ``datetime``, but that normalises the form — basic vs extended notation, ``Z``
    vs ``TZID`` — and so changes bytes we promised not to change.  ``vText``
    round-trips the stored string unchanged (no RFC 5545 special character can
    appear in a date value), and the value-type parameters are restored from the
    shape of the value itself.
    """
    if name in todo:
        del todo[name]
    if value is None:
        return

    todo.add(name, vText(value), encode=False)
    params = todo[name].params
    if "T" not in value:
        # A DATE value is not the default type for DTSTART/DUE/COMPLETED, so
        # VALUE=DATE is required or every other client reads it as a DATE-TIME.
        params["VALUE"] = "DATE"
    elif tzid and not value.endswith("Z"):
        params["TZID"] = tzid


def _apply_owned_properties(todo: ITodo, task: Task) -> None:
    _set(todo, "UID", task.uid)
    _set(todo, "SUMMARY", task.summary)
    _set(todo, "DESCRIPTION", task.description)
    _set(todo, "LOCATION", task.location)
    _set(todo, "URL", task.url)
    _set(todo, "STATUS", task.status.value if task.status else None)
    # PRIORITY is omitted entirely when undefined; 0 would mean the same thing
    # but is written by clients that treat 0 as "highest".
    _set(todo, "PRIORITY", task.priority)
    _set(todo, "PERCENT-COMPLETE", task.percent_complete)
    # SEQUENCE defaults to 0 in RFC 5545, so writing SEQUENCE:0 into a resource
    # that never had one is pure churn in every other client's diff.
    if task.sequence or "SEQUENCE" in todo:
        _set(todo, "SEQUENCE", task.sequence)
    _set(todo, X_ORDER, task.davpunk_order)
    _set(todo, X_KANBAN, task.kanban_col)

    _set_raw(todo, "DTSTART", task.dtstart_value, task.dtstart_tzid)
    _set_raw(todo, "DUE", task.due_value, task.due_tzid)
    _set_raw(todo, "COMPLETED", _utc_stamp(task.completed))
    _set_raw(todo, "LAST-MODIFIED", _utc_stamp(task.last_modified) or _now_stamp())

    if "CATEGORIES" in todo:
        del todo["CATEGORIES"]
    if task.categories:
        todo.add("CATEGORIES", task.categories)

    if "RELATED-TO" in todo:
        del todo["RELATED-TO"]
    if task.parent_uid:
        todo.add("RELATED-TO", task.parent_uid, parameters={"RELTYPE": "PARENT"})


def _rewrite_alarms(todo: ITodo, task: Task) -> None:
    """Regenerate every VALARM from the ``valarms`` table.

    VALARM subcomponents pass through untouched unless the user edited alarms
    in this transaction, which is exactly what ``rewrite_alarms=True`` means.
    """
    from icalendar import Alarm as IAlarm

    todo.subcomponents = [c for c in todo.subcomponents if c.name != "VALARM"]
    for alarm in task.alarms:
        component = IAlarm()
        component.add("ACTION", "DISPLAY")
        component.add("DESCRIPTION", task.summary or "Reminder")
        component.add("TRIGGER", timedelta(seconds=alarm.trigger_offset))
        component["TRIGGER"].params["RELATED"] = alarm.related.value
        todo.add_component(component)


def _ensure_vtimezones(calendar: ICalendar, task: Task) -> None:
    """Add a VTIMEZONE for a TZID the resource does not define.

    Never prunes an apparently-unreferenced one: another client's RECURRENCE-ID
    or a property DavPunk does not model may depend on it.
    """
    present = {str(tz.get("TZID")) for tz in calendar.walk("VTIMEZONE")}
    for tzid in (task.due_tzid, task.dtstart_tzid):
        if tzid and tzid not in present:
            component = _build_vtimezone(tzid)
            if component is not None:
                calendar.subcomponents.insert(0, component)
                present.add(tzid)


def _missing_vtimezones(calendar: ICalendar, task: Task) -> list[str]:
    """The same check, rendered as text for the splice path."""
    present = {str(tz.get("TZID")) for tz in calendar.walk("VTIMEZONE")}
    out: list[str] = []
    for tzid in (task.due_tzid, task.dtstart_tzid):
        if tzid and tzid not in present:
            component = _build_vtimezone(tzid)
            if component is not None:
                out.append(component.to_ical(sorted=False).decode("utf-8"))
                present.add(tzid)
    return out


def _build_vtimezone(tzid: str) -> ITimezone | None:
    """A minimal, standards-valid VTIMEZONE generated from ``zoneinfo``.

    Deliberately minimal: one STANDARD (and one DAYLIGHT where the zone
    observes it) covering the current offsets.  A full historical transition
    table is not needed for a client to resolve the TZID, and generating one
    from ``zoneinfo`` is not possible without private APIs.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("Cannot generate a VTIMEZONE for unresolvable TZID %r", tzid)
        return None

    from icalendar import TimezoneDaylight, TimezoneStandard

    winter = datetime(date.today().year, 1, 15, 12, 0, tzinfo=zone)
    summer = datetime(date.today().year, 7, 15, 12, 0, tzinfo=zone)

    component = ITimezone()
    component.add("TZID", tzid)

    standard = TimezoneStandard()
    standard.add("DTSTART", datetime(1970, 1, 1, 0, 0))
    standard.add("TZOFFSETFROM", winter.utcoffset())
    standard.add("TZOFFSETTO", winter.utcoffset())
    standard.add("TZNAME", winter.tzname() or tzid)
    component.add_component(standard)

    if summer.utcoffset() != winter.utcoffset():
        daylight = TimezoneDaylight()
        daylight.add("DTSTART", datetime(1970, 1, 1, 0, 0))
        daylight.add("TZOFFSETFROM", winter.utcoffset())
        daylight.add("TZOFFSETTO", summer.utcoffset())
        daylight.add("TZNAME", summer.tzname() or tzid)
        component.add_component(daylight)

    return component


def _utc_stamp(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    from datetime import UTC

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y%m%dT%H%M%SZ")


def _now_stamp() -> str:
    from datetime import UTC

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def new_resource(task: Task) -> str:
    """Serialize a task DavPunk is creating; PRODID is ours only here."""
    fresh = task.model_copy(deep=True)
    fresh.raw_ics = None
    fresh.read_only_reason = None
    return serialize_task(fresh, rewrite_alarms=bool(fresh.alarms))


def uid_of(raw_ics: str | bytes) -> str | None:
    """The UID without a full parse — used by the create path's 412 probe."""
    try:
        return parse_resource(raw_ics, max_resource_bytes=1 << 62).uid or None
    except ICalParseError:
        return None
