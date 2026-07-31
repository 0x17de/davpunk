"""Parsing: fields, VALARM rules, durations, edge shapes."""

from __future__ import annotations

import pytest

from davpunk.core.ical_parser import (
    new_resource,
    parse_duration_seconds,
    parse_resource,
    serialize_task,
)
from davpunk.models.task import Alarm, AlarmRelated, Status, Task


def todo(*body: str, calendar_extra: str = "") -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//X//EN\r\n"
        + calendar_extra
        + "BEGIN:VTODO\r\nUID:t-1\r\n"
        + "".join(line + "\r\n" for line in body)
        + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


# ------------------------------------------------------------------- statuses


@pytest.mark.parametrize("status", ["NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"])
def test_every_status_parses(status):
    assert parse_resource(todo(f"STATUS:{status}")).status == Status(status)


def test_an_unknown_status_leaves_the_field_unset_rather_than_raising():
    assert parse_resource(todo("STATUS:INVENTED")).status is None


def test_a_missing_status_is_none():
    assert parse_resource(todo("SUMMARY:S")).status is None


# ------------------------------------------------------------------ priority


def test_priority_zero_parses_as_undefined():
    """RFC 5545: 0 means undefined, not highest."""
    assert parse_resource(todo("PRIORITY:0")).priority is None


def test_a_malformed_property_value_raises_ical_parse_error():
    """icalendar has no lenient mode: ``PRIORITY:high`` makes the whole resource
    unparseable.  The contract is that it surfaces as ICalParseError and not as
    some arbitrary library exception, so the pull phase can skip that one href,
    log it, and carry on with the rest of the collection."""
    from davpunk.core.ical_parser import ICalParseError

    with pytest.raises(ICalParseError, match="Expected int"):
        parse_resource(todo("PRIORITY:high"))


def test_priority_one_through_nine():
    for value in range(1, 10):
        assert parse_resource(todo(f"PRIORITY:{value}")).priority == value


# ---------------------------------------------------------------- categories


def test_categories_split_on_commas():
    assert parse_resource(todo("CATEGORIES:work,urgent,q3")).categories == [
        "work",
        "urgent",
        "q3",
    ]


def test_repeated_categories_properties_merge():
    assert sorted(parse_resource(todo("CATEGORIES:work", "CATEGORIES:urgent")).categories) == [
        "urgent",
        "work",
    ]


def test_no_categories_is_an_empty_list():
    assert parse_resource(todo("SUMMARY:S")).categories == []


# ---------------------------------------------------------------- date values


def test_a_date_value_keeps_its_basic_form():
    assert parse_resource(todo("DUE;VALUE=DATE:20260731")).due_value == "20260731"


def test_a_utc_value_keeps_its_z():
    assert parse_resource(todo("DUE:20260731T170000Z")).due_value == "20260731T170000Z"


def test_a_tzid_value_keeps_value_and_tzid_separately():
    task = parse_resource(todo("DUE;TZID=Asia/Tokyo:20260731T170000"))
    assert task.due_value == "20260731T170000"
    assert task.due_tzid == "Asia/Tokyo"


def test_a_floating_value_has_no_tzid():
    task = parse_resource(todo("DUE:20260731T170000"))
    assert task.due_value == "20260731T170000"
    assert task.due_tzid is None


def test_the_sort_key_is_derived_but_not_authoritative():
    task = parse_resource(todo("DUE;VALUE=DATE:20260731"))
    assert task.due == 1785456000  # 00:00 UTC — coarse, by design
    assert task.due_value == "20260731"


def test_completed_is_stored_as_an_exact_epoch():
    task = parse_resource(todo("COMPLETED:20260731T170000Z"))
    assert task.completed == 1785517200


# --------------------------------------------------------------------- alarms


def test_a_relative_alarm_with_a_due_anchor_is_kept():
    task = parse_resource(
        todo(
            "DUE:20260731T170000Z",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER;RELATED=END:-PT1H",
            "END:VALARM",
        )
    )
    assert task.alarms == [Alarm(related=AlarmRelated.END, trigger_offset=-3600)]


def test_an_alarm_with_no_anchor_is_dropped_at_parse():
    """A relative trigger with nothing to be relative to is invalid."""
    task = parse_resource(
        todo(
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER;RELATED=END:-PT1H",
            "END:VALARM",
        )
    )
    assert task.alarms == []


def test_a_start_related_alarm_needs_a_dtstart():
    body = [
        "DUE:20260731T170000Z",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:R",
        "TRIGGER;RELATED=START:-PT15M",
        "END:VALARM",
    ]
    assert parse_resource(todo(*body)).alarms == []

    with_start = ["DTSTART:20260731T090000Z", *body]
    assert parse_resource(todo(*with_start)).alarms == [
        Alarm(related=AlarmRelated.START, trigger_offset=-900)
    ]


def test_an_absolute_trigger_is_out_of_scope():
    task = parse_resource(
        todo(
            "DUE:20260731T170000Z",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:R",
            "TRIGGER;VALUE=DATE-TIME:20260731T160000Z",
            "END:VALARM",
        )
    )
    assert task.alarms == []


def test_an_email_alarm_is_ignored():
    task = parse_resource(
        todo(
            "DUE:20260731T170000Z",
            "BEGIN:VALARM",
            "ACTION:EMAIL",
            "DESCRIPTION:R",
            "SUMMARY:S",
            "ATTENDEE:mailto:a@example.com",
            "TRIGGER;RELATED=END:-PT1H",
            "END:VALARM",
        )
    )
    assert task.alarms == []


def test_alarms_pass_through_untouched_unless_rewritten():
    """VALARMs survive an edit that did not touch them."""
    original = todo(
        "DUE:20260731T170000Z",
        "SUMMARY:Before",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Custom wording nobody else would generate",
        "TRIGGER;RELATED=END:-PT1H",
        "X-CUSTOM-ALARM-PROP:kept",
        "END:VALARM",
    )
    task = parse_resource(original)
    task.summary = "After"
    out = serialize_task(task)

    assert "X-CUSTOM-ALARM-PROP:kept" in out
    assert "DESCRIPTION:Custom wording nobody else would generate" in out


def test_rewrite_alarms_regenerates_the_whole_set():
    task = parse_resource(
        todo(
            "DUE:20260731T170000Z",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:old",
            "TRIGGER;RELATED=END:-PT1H",
            "END:VALARM",
        )
    )
    task.alarms = [Alarm(related=AlarmRelated.END, trigger_offset=-900)]
    out = serialize_task(task, rewrite_alarms=True)

    assert out.count("BEGIN:VALARM") == 1
    assert parse_resource(out).alarms == [Alarm(related=AlarmRelated.END, trigger_offset=-900)]


# ------------------------------------------------------------------ durations


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("-PT1H", -3600),
        ("PT15M", 900),
        ("-PT15M", -900),
        ("-P1D", -86400),
        ("P1W", 604800),
        ("-PT1H30M", -5400),
        ("PT30S", 30),
        ("-P1DT2H", -93600),
    ],
)
def test_duration_parsing(text, seconds):
    assert parse_duration_seconds(text) == seconds


@pytest.mark.parametrize("text", ["", "P", "nonsense", "1H", "-P"])
def test_unparseable_durations_return_none(text):
    assert parse_duration_seconds(text) is None


# ------------------------------------------------------------------ x-props


def test_x_davpunk_properties_round_trip():
    task = parse_resource(todo("X-DAVPUNK-ORDER:3000", "X-DAVPUNK-KANBAN-COL:inprogress"))
    assert task.davpunk_order == 3000
    assert task.kanban_col == "inprogress"

    out = serialize_task(task)
    assert "X-DAVPUNK-ORDER:3000" in out
    assert "X-DAVPUNK-KANBAN-COL:inprogress" in out


def test_a_non_numeric_order_is_ignored():
    assert parse_resource(todo("X-DAVPUNK-ORDER:abc")).davpunk_order is None


# -------------------------------------------------------------------- misc


def test_a_vevent_only_resource_has_no_vtodo():
    """Mixed collections exist; events are simply not ours."""
    from davpunk.core.ical_parser import ICalParseError

    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(ICalParseError):
        parse_resource(ics)


def test_an_empty_summary_is_none_not_empty_string():
    assert parse_resource(todo("SUMMARY:")).summary is None


def test_a_new_resource_needs_no_raw_ics():
    out = new_resource(Task(uid="fresh", summary="Fresh task"))
    assert "BEGIN:VTODO" in out
    assert parse_resource(out).summary == "Fresh task"


def test_a_new_resource_with_alarms_writes_them():
    task = Task(uid="fresh", summary="S", due_value="20260731T170000Z")
    task.alarms = [Alarm(related=AlarmRelated.END, trigger_offset=-3600)]
    assert parse_resource(new_resource(task)).alarms == task.alarms


def test_uid_of_reads_the_uid_without_a_size_limit():
    from davpunk.core.ical_parser import uid_of

    assert uid_of(todo("SUMMARY:S")) == "t-1"
    assert uid_of("not iCalendar") is None
