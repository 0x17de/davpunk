"""Read-only quarantine.

``multipart`` is what makes "RRULE deferred, opaque round-trip" actually safe: a
recurring series with ``RECURRENCE-ID`` overrides lives in one resource as
several VTODOs, and re-serializing the master would destroy the overrides.
"""

from __future__ import annotations

import pytest

from davpunk.core import cache
from davpunk.core.cache import ReadOnlyResourceError
from davpunk.core.ical_parser import (
    DEFAULT_MAX_RESOURCE_BYTES,
    ICalParseError,
    parse_resource,
    serialize_task,
)
from davpunk.models.task import ReadOnlyReason, SyncState

RECURRING = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Another Client//EN\r\n"
    "BEGIN:VTODO\r\n"
    "UID:series-1\r\n"
    "SUMMARY:Weekly report\r\n"
    "DTSTART:20260701T090000Z\r\n"
    "DUE:20260701T170000Z\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
    "STATUS:NEEDS-ACTION\r\n"
    "END:VTODO\r\n"
    "BEGIN:VTODO\r\n"
    "UID:series-1\r\n"
    "RECURRENCE-ID:20260708T090000Z\r\n"
    "SUMMARY:Weekly report (moved)\r\n"
    "DTSTART:20260709T090000Z\r\n"
    "DUE:20260709T170000Z\r\n"
    "STATUS:COMPLETED\r\n"
    "END:VTODO\r\n"
    "END:VCALENDAR\r\n"
)

SIMPLE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//X//EN\r\n"
    "BEGIN:VTODO\r\nUID:plain-1\r\nSUMMARY:{summary}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
)


# ------------------------------------------------------------------ detection


def test_more_than_one_vtodo_is_quarantined():
    assert parse_resource(RECURRING).read_only_reason is ReadOnlyReason.MULTIPART


def test_the_master_supplies_the_displayed_fields():
    """The master is the VTODO *without* a RECURRENCE-ID."""
    task = parse_resource(RECURRING)
    assert task.summary == "Weekly report"
    assert task.rrule == "FREQ=WEEKLY;BYDAY=MO"


def test_raw_ics_is_always_the_complete_vcalendar():
    assert parse_resource(RECURRING).raw_ics == RECURRING


def test_a_single_vtodo_is_not_quarantined():
    assert parse_resource(SIMPLE.format(summary="Plain")).read_only_reason is None


def test_an_oversize_resource_is_quarantined():
    big = SIMPLE.format(summary="x" * 5000)
    assert parse_resource(big, max_resource_bytes=1024).read_only_reason is (
        ReadOnlyReason.OVERSIZE
    )


def test_the_oversize_threshold_defaults_to_256_kib():
    assert DEFAULT_MAX_RESOURCE_BYTES == 262_144


def test_multipart_wins_over_oversize_when_both_apply():
    """Both forbid re-serialization; naming the structural cause is more useful."""
    assert parse_resource(RECURRING, max_resource_bytes=10).read_only_reason is (
        ReadOnlyReason.MULTIPART
    )


def test_the_size_check_counts_octets_not_characters():
    ics = SIMPLE.format(summary="→" * 100)  # 300 bytes of summary
    assert parse_resource(ics, max_resource_bytes=len(ics) - 1).read_only_reason is (
        ReadOnlyReason.OVERSIZE
    )
    assert parse_resource(ics, max_resource_bytes=len(ics.encode())).read_only_reason is None


# --------------------------------------------------------------- enforcement


def test_serializing_a_quarantined_task_is_refused():
    """The single guard that makes the whole quarantine meaningful."""
    with pytest.raises(ICalParseError, match="multipart"):
        serialize_task(parse_resource(RECURRING))


def test_serializing_an_oversize_task_is_refused():
    big = SIMPLE.format(summary="x" * 5000)
    with pytest.raises(ICalParseError, match="oversize"):
        serialize_task(parse_resource(big, max_resource_bytes=1024))


def test_editing_a_quarantined_task_is_refused(conn, calendar_id):
    parsed = parse_resource(RECURRING)
    parsed.calendar_id = calendar_id
    parsed.href = "series-1.ics"
    with cache.tx(conn):
        task_id = cache.insert_server_task(parsed, 'W/"v1"', conn)

    with pytest.raises(ReadOnlyResourceError) as excinfo:
        cache.update_task_optimistic(task_id, {"summary": "nope"}, conn)
    assert excinfo.value.reason == "multipart"


def test_deleting_a_quarantined_task_is_allowed(conn, calendar_id):
    """DELETE removes the whole resource; no serialization is involved."""
    parsed = parse_resource(RECURRING)
    parsed.calendar_id = calendar_id
    parsed.href = "series-1.ics"
    with cache.tx(conn):
        task_id = cache.insert_server_task(parsed, 'W/"v1"', conn)

    cache.delete_task_local(task_id, conn)
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.PENDING_DELETE.value


def test_moving_a_quarantined_task_is_allowed(conn, calendar_id, other_calendar_id):
    """A move relocates the bytes verbatim."""
    parsed = parse_resource(RECURRING)
    parsed.calendar_id = calendar_id
    parsed.href = "series-1.ics"
    with cache.tx(conn):
        task_id = cache.insert_server_task(parsed, 'W/"v1"', conn)

    cache.move_task_local(task_id, other_calendar_id, conn)

    row = cache.get_task_row(task_id, conn)
    assert row["calendar_id"] == other_calendar_id
    # The bytes a move transmits are exactly the bytes we received.
    assert row["raw_ics"] == RECURRING


def test_the_push_scan_lets_delete_and_move_through_but_not_update(conn, calendar_id):
    parsed = parse_resource(RECURRING)
    parsed.calendar_id = calendar_id
    parsed.href = "series-1.ics"
    with cache.tx(conn):
        task_id = cache.insert_server_task(parsed, 'W/"v1"', conn)

    for change_type, expected in (("update", 0), ("create", 0), ("delete", 1), ("move", 1)):
        with cache.tx(conn):
            cache._queue(conn, task_id, change_type, 'W/"v1"')
        assert len(cache.ready_pending_changes(calendar_id, conn)) == expected, change_type


def test_a_quarantined_resource_survives_a_full_cycle_byte_identical(
    conn, calendar_id, other_calendar_id
):
    """Import, move, and read back — never re-serialized, so never changed."""
    parsed = parse_resource(RECURRING)
    parsed.calendar_id = calendar_id
    parsed.href = "series-1.ics"
    with cache.tx(conn):
        task_id = cache.insert_server_task(parsed, 'W/"v1"', conn)

    cache.move_task_local(task_id, other_calendar_id, conn)
    assert cache.get_task(task_id, conn).raw_ics == RECURRING
