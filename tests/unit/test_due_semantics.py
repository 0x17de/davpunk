"""Due semantics: DATE / TZID / floating / UTC overdue boundaries.

The bug this file exists to prevent: comparing ``tasks.due`` (a coarse sort key
where a DATE maps to 00:00 UTC) against ``now()`` marks a task due today as
overdue as soon as the UTC day starts — up to a full day early.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from davpunk.models.task import (
    Alarm,
    AlarmRelated,
    Status,
    Task,
    dtstart_deadline,
    due_deadline,
    local_day_bounds,
    next_local_midnight,
    sort_epoch,
)

BERLIN = ZoneInfo("Europe/Berlin")  # UTC+2 in July
TOKYO = ZoneInfo("Asia/Tokyo")  # UTC+9, always


def test_date_value_is_overdue_only_after_end_of_that_local_day():
    task = Task(uid="a", due_value="20260731")
    deadline = due_deadline(task, BERLIN)

    assert deadline.date() == date(2026, 7, 31)
    assert deadline.hour == 23 and deadline.minute == 59 and deadline.second == 59
    assert deadline.tzinfo is BERLIN

    # 23:00 local on the due date is NOT overdue...
    assert not task.is_overdue(datetime(2026, 7, 31, 23, 0, tzinfo=BERLIN), tz=BERLIN)
    # ...but one minute past midnight the next day is.
    assert task.is_overdue(datetime(2026, 8, 1, 0, 1, tzinfo=BERLIN), tz=BERLIN)


def test_date_value_not_overdue_at_utc_midnight_of_the_due_day():
    """The exact regression T69 names.

    In Berlin (UTC+2) the UTC day begins at 02:00 local.  A naive comparison
    against the sort key would call this overdue 22 hours early.
    """
    task = Task(uid="a", due_value="20260731")
    utc_midnight = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)

    assert sort_epoch("20260731", None) == int(utc_midnight.timestamp())
    assert not task.is_overdue(utc_midnight.astimezone(BERLIN), tz=BERLIN)


def test_datetime_with_tzid_uses_that_zone():
    task = Task(uid="a", due_value="20260731T090000", due_tzid="Asia/Tokyo")
    deadline = due_deadline(task, BERLIN)

    assert deadline == datetime(2026, 7, 31, 9, 0, tzinfo=TOKYO)
    # 09:00 Tokyo is 02:00 Berlin; 03:00 Berlin is past it.
    assert task.is_overdue(datetime(2026, 7, 31, 3, 0, tzinfo=BERLIN), tz=BERLIN)
    assert not task.is_overdue(datetime(2026, 7, 31, 1, 0, tzinfo=BERLIN), tz=BERLIN)


def test_utc_z_suffix_is_utc_regardless_of_local_zone():
    task = Task(uid="a", due_value="20260731T120000Z")
    assert due_deadline(task, TOKYO) == datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def test_floating_time_is_local_by_definition():
    task = Task(uid="a", due_value="20260731T120000")  # no TZID, no Z
    assert due_deadline(task, BERLIN) == datetime(2026, 7, 31, 12, 0, tzinfo=BERLIN)
    assert due_deadline(task, TOKYO) == datetime(2026, 7, 31, 12, 0, tzinfo=TOKYO)


def test_unresolvable_tzid_falls_back_to_local_for_the_deadline():
    task = Task(uid="a", due_value="20260731T120000", due_tzid="Mars/Olympus_Mons")
    assert due_deadline(task, BERLIN) == datetime(2026, 7, 31, 12, 0, tzinfo=BERLIN)
    # ...and to UTC for the coarse sort key only.
    assert sort_epoch("20260731T120000", "Mars/Olympus_Mons") == int(
        datetime(2026, 7, 31, 12, 0, tzinfo=UTC).timestamp()
    )


def test_no_due_value_is_never_overdue():
    task = Task(uid="a")
    assert due_deadline(task, BERLIN) is None
    assert not task.is_overdue(datetime.now(BERLIN), tz=BERLIN)


@pytest.mark.parametrize("status", [Status.COMPLETED, Status.CANCELLED])
def test_completed_and_cancelled_tasks_are_never_overdue(status):
    task = Task(uid="a", due_value="20200101", status=status)
    assert not task.is_overdue(datetime(2026, 7, 31, tzinfo=BERLIN), tz=BERLIN)


def test_dtstart_date_uses_the_same_end_of_day_rule():
    task = Task(uid="a", dtstart_value="20260731")
    deadline = dtstart_deadline(task, BERLIN)
    assert deadline.date() == date(2026, 7, 31) and deadline.hour == 23


def test_extended_iso_date_form_is_accepted():
    task = Task(uid="a", due_value="2026-07-31")
    assert due_deadline(task, BERLIN).date() == date(2026, 7, 31)


def test_local_day_bounds_span_exactly_one_local_day():
    start, end = local_day_bounds(date(2026, 7, 31), BERLIN)
    assert end - start == 86400
    assert datetime.fromtimestamp(start, BERLIN).hour == 0


def test_next_local_midnight_is_the_upcoming_one():
    now = datetime(2026, 7, 31, 23, 30, tzinfo=BERLIN)
    assert next_local_midnight(now, BERLIN) == datetime(2026, 8, 1, 0, 0, tzinfo=BERLIN)


def test_dst_day_bounds_are_not_24h():
    """Europe/Berlin springs forward on 2026-03-29; that local day is 23 h."""
    start, end = local_day_bounds(date(2026, 3, 29), BERLIN)
    assert end - start == 23 * 3600


# --------------------------------------------------------------------- alarms


def test_alarm_anchored_to_due_is_relative_to_the_start_of_a_date_value():
    task = Task(uid="a", due_value="20260731")
    alarm = Alarm(related=AlarmRelated.END, trigger_offset=-3600)
    # The *anchor* is the value's own instant (00:00), not the overdue deadline.
    assert alarm.trigger_at(task, BERLIN) == datetime(2026, 7, 30, 23, 0, tzinfo=BERLIN)


def test_alarm_with_no_anchor_yields_no_trigger():
    task = Task(uid="a")
    assert Alarm(related=AlarmRelated.END, trigger_offset=-3600).trigger_at(task, BERLIN) is None


def test_alarm_anchored_to_dtstart():
    task = Task(uid="a", dtstart_value="20260731T080000", dtstart_tzid="Europe/Berlin")
    alarm = Alarm(related=AlarmRelated.START, trigger_offset=-900)
    assert alarm.trigger_at(task, BERLIN) == datetime(2026, 7, 31, 7, 45, tzinfo=BERLIN)


def test_alarm_offset_crossing_a_dst_boundary_is_absolute_seconds():
    task = Task(uid="a", dtstart_value="20260329T120000", dtstart_tzid="Europe/Berlin")
    alarm = Alarm(
        related=AlarmRelated.START, trigger_offset=-int(timedelta(days=1).total_seconds())
    )
    fired = alarm.trigger_at(task, BERLIN)
    # 24 h before 12:00 on a 23-hour day lands at 11:00 the previous day.
    assert fired.astimezone(BERLIN) == datetime(2026, 3, 28, 11, 0, tzinfo=BERLIN)
