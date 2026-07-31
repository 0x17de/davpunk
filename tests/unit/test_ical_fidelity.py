"""VCALENDAR-level preservation.

The failure this file guards against: an implementation that rebuilds the
wrapper around the VTODO it cares about, dropping the ``VTIMEZONE`` definitions
and so breaking every ``TZID=`` reference for every other client sharing the
collection.
"""

from __future__ import annotations

import pytest

from davpunk.core.ical_parser import (
    PRODID,
    ICalParseError,
    new_resource,
    parse_resource,
    serialize_task,
)
from davpunk.models.task import Status, Task

RICH = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Some Other Client//Their Product 4.2//EN\r\n"
    "CALSCALE:GREGORIAN\r\n"
    "METHOD:PUBLISH\r\n"
    "X-WR-CALNAME:Work\r\n"
    "X-WR-TIMEZONE:Europe/Berlin\r\n"
    "BEGIN:VTIMEZONE\r\n"
    "TZID:Europe/Berlin\r\n"
    "BEGIN:DAYLIGHT\r\n"
    "TZOFFSETFROM:+0100\r\n"
    "TZOFFSETTO:+0200\r\n"
    "TZNAME:CEST\r\n"
    "DTSTART:19700329T020000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\n"
    "END:DAYLIGHT\r\n"
    "BEGIN:STANDARD\r\n"
    "TZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0100\r\n"
    "TZNAME:CET\r\n"
    "DTSTART:19701025T030000\r\n"
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE\r\n"
    "BEGIN:VTODO\r\n"
    "UID:abc-123\r\n"
    "SUMMARY:Fix login bug\r\n"
    "DESCRIPTION:Repro steps below\r\n"
    "STATUS:IN-PROCESS\r\n"
    "PRIORITY:2\r\n"
    "PERCENT-COMPLETE:60\r\n"
    "DUE;TZID=Europe/Berlin:20260731T170000\r\n"
    "CATEGORIES:work,urgent\r\n"
    "X-FOREIGN-PROPERTY:do not touch me\r\n"
    "X-APPLE-SORT-ORDER:12345\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)


def lines(ics: str) -> list[str]:
    return ics.split("\r\n")


# ------------------------------------------------------ calendar-level survival


def test_calendar_level_properties_survive():
    task = parse_resource(RICH)
    out = serialize_task(task)
    for prop in ("VERSION:2.0", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Work"):
        assert prop in lines(out)


def test_a_foreign_prodid_is_never_rewritten():
    """PRODID identifies the creating client; only a resource DavPunk creates
    carries ours."""
    out = serialize_task(parse_resource(RICH))
    assert "PRODID:-//Some Other Client//Their Product 4.2//EN" in lines(out)
    assert PRODID not in out


def test_a_resource_davpunk_creates_carries_our_prodid():
    task = Task(uid="new-1", summary="Fresh")
    assert f"PRODID:{PRODID}" in lines(new_resource(task))


def test_vtimezone_survives_intact():
    out = serialize_task(parse_resource(RICH))
    assert "BEGIN:VTIMEZONE" in out
    assert "TZID:Europe/Berlin" in lines(out)
    assert "TZNAME:CEST" in lines(out)
    assert "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU" in lines(out)


def test_foreign_vtodo_properties_survive():
    out = serialize_task(parse_resource(RICH))
    assert "X-FOREIGN-PROPERTY:do not touch me" in lines(out)
    assert "X-APPLE-SORT-ORDER:12345" in lines(out)


def test_an_untouched_resource_round_trips_byte_for_byte():
    """Nothing semantic changed, so nothing textual should either — except
    LAST-MODIFIED, which every write updates by definition."""
    task = parse_resource(RICH)
    task.last_modified = 1_700_000_000
    out = serialize_task(task)
    without_lm = [ln for ln in lines(out) if not ln.startswith("LAST-MODIFIED")]
    assert without_lm == [ln for ln in lines(RICH) if ln != ""] + [""]


def test_property_order_is_preserved_when_a_value_changes():
    task = parse_resource(RICH)
    task.summary = "Fix login bug (updated)"
    out = serialize_task(task)

    before = [ln.split(":")[0].split(";")[0] for ln in lines(RICH) if ln.strip()]
    after = [ln.split(":")[0].split(";")[0] for ln in lines(out) if ln.strip()]
    # LAST-MODIFIED is new; everything that existed keeps its relative order.
    assert [p for p in after if p in before] == before


# ---------------------------------------------------------------- date shapes


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("20260731", "DUE;VALUE=DATE:20260731"),
        ("20260731T170000Z", "DUE:20260731T170000Z"),
        ("20260731T170000", "DUE:20260731T170000"),
    ],
)
def test_the_three_unparameterised_due_shapes_round_trip(value, line):
    task = Task(uid="a", due_value=value)
    assert line in lines(new_resource(task))


def test_a_tzid_due_round_trips_with_its_parameter():
    task = Task(uid="a", due_value="20260731T170000", due_tzid="Europe/Berlin")
    assert "DUE;TZID=Europe/Berlin:20260731T170000" in lines(new_resource(task))


def test_the_original_value_is_authoritative_not_the_sort_key():
    """The epoch columns are coarse and are never serialized."""
    task = parse_resource(RICH)
    assert task.due_value == "20260731T170000"
    task.due = 0  # deliberately wrong sort key
    assert "DUE;TZID=Europe/Berlin:20260731T170000" in lines(serialize_task(task))


def test_a_tzid_with_no_vtimezone_gets_one_generated():
    task = Task(uid="a", due_value="20260731T170000", due_tzid="Europe/Berlin")
    out = new_resource(task)
    assert "BEGIN:VTIMEZONE" in out
    assert "TZID:Europe/Berlin" in lines(out)


def test_an_existing_vtimezone_is_not_duplicated():
    task = parse_resource(RICH)
    out = serialize_task(task)
    assert out.count("BEGIN:VTIMEZONE") == 1


def test_an_unreferenced_vtimezone_is_never_pruned():
    """Another client's RECURRENCE-ID may depend on it."""
    task = parse_resource(RICH)
    task.due_value = None
    task.due_tzid = None
    assert "TZID:Europe/Berlin" in lines(serialize_task(task))


# -------------------------------------------------------------- CRLF, folding


def test_output_uses_crlf():
    out = serialize_task(parse_resource(RICH))
    assert "\r\n" in out
    assert "\n" not in out.replace("\r\n", "")


def test_long_lines_are_folded_at_75_octets():
    task = Task(uid="a", summary="x" * 300)
    out = new_resource(task)
    for line in out.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_folding_never_splits_a_multibyte_codepoint():
    """75 **octets**, not characters.  A naive character-count fold splits a
    multi-byte sequence and the resource stops being valid UTF-8."""
    task = Task(uid="a", description="→" * 200)  # 3 bytes each
    out = new_resource(task)

    for line in out.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    # Unfold and confirm the text survived exactly.
    unfolded = out.replace("\r\n ", "").replace("\r\n\t", "")
    assert "→" * 200 in unfolded
    assert parse_resource(out).description == "→" * 200


def test_a_multibyte_summary_round_trips():
    task = Task(uid="a", summary="日本語のタスク — with an em dash")
    assert parse_resource(new_resource(task)).summary == "日本語のタスク — with an em dash"


def test_escaped_characters_in_a_description_round_trip():
    text = "Line one\nLine two; with a semicolon, a comma and a \\ backslash"
    task = Task(uid="a", description=text)
    assert parse_resource(new_resource(task)).description == text


# ------------------------------------------------------------------- RRULE


def test_rrule_is_opaque_pass_through():
    ics = RICH.replace("UID:abc-123\r\n", "UID:abc-123\r\nRRULE:FREQ=WEEKLY;COUNT=10\r\n")
    task = parse_resource(ics)
    assert task.rrule == "FREQ=WEEKLY;COUNT=10"
    assert "RRULE:FREQ=WEEKLY;COUNT=10" in lines(serialize_task(task))


# ------------------------------------------------------------------- fields


def test_all_modelled_fields_round_trip():
    task = Task(
        uid="round-trip",
        summary="Everything",
        description="A description",
        status=Status.IN_PROCESS,
        priority=3,
        percent_complete=40,
        due_value="20260731T170000",
        due_tzid="Europe/Berlin",
        dtstart_value="20260701",
        url="https://example.com/ticket/1",
        location="Berlin",
        parent_uid="the-parent",
        davpunk_order=2000,
        kanban_col="inprogress",
        sequence=3,
        categories=["work", "urgent"],
    )
    back = parse_resource(new_resource(task))

    assert back.uid == "round-trip"
    assert back.summary == "Everything"
    assert back.status is Status.IN_PROCESS
    assert back.priority == 3
    assert back.percent_complete == 40
    assert back.due_value == "20260731T170000"
    assert back.due_tzid == "Europe/Berlin"
    assert back.dtstart_value == "20260701"
    assert back.url == "https://example.com/ticket/1"
    assert back.location == "Berlin"
    assert back.parent_uid == "the-parent"
    assert back.davpunk_order == 2000
    assert back.kanban_col == "inprogress"
    assert back.sequence == 3
    assert sorted(back.categories) == ["urgent", "work"]


def test_priority_undefined_is_omitted_entirely():
    """Writing PRIORITY:0 would be read as "highest" by clients that get T61
    wrong; omitting it cannot be misread."""
    out = new_resource(Task(uid="a", summary="s"))
    assert not any(ln.startswith("PRIORITY") for ln in lines(out))


def test_only_the_first_parent_relation_is_honored():
    ics = RICH.replace(
        "UID:abc-123\r\n",
        "UID:abc-123\r\n"
        "RELATED-TO;RELTYPE=PARENT:first-parent\r\n"
        "RELATED-TO;RELTYPE=PARENT:second-parent\r\n",
    )
    assert parse_resource(ics).parent_uid == "first-parent"


def test_a_sibling_relation_is_not_a_parent():
    ics = RICH.replace("UID:abc-123\r\n", "UID:abc-123\r\nRELATED-TO;RELTYPE=SIBLING:a-sibling\r\n")
    assert parse_resource(ics).parent_uid is None


def test_a_resource_with_no_vtodo_is_refused():
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(ICalParseError):
        parse_resource(ics)


def test_garbage_is_refused_rather_than_crashing():
    with pytest.raises(ICalParseError):
        parse_resource("this is not iCalendar at all")
