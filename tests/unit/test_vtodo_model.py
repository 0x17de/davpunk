"""The Task model: canonicalization, PRIORITY 0 → NULL, ordering."""

from __future__ import annotations

import pytest

from davpunk.models.calendar import Calendar, calendar_id
from davpunk.models.task import (
    Status,
    Task,
    canonicalize_fields,
    rebalance_orders,
    utc_now_epoch,
)

# ------------------------------------------------------------ canonicalization


def test_completed_sets_timestamp_and_forces_percent_100():
    task = Task(uid="a", status=Status.COMPLETED, percent_complete=60).canonicalized()
    assert task.percent_complete == 100
    assert task.completed is not None


def test_completed_preserves_an_existing_timestamp():
    task = Task(uid="a", status=Status.COMPLETED, completed=1_000_000).canonicalized()
    assert task.completed == 1_000_000


@pytest.mark.parametrize("status", [Status.NEEDS_ACTION, Status.IN_PROCESS])
def test_open_statuses_clear_completed_but_keep_percent(status):
    task = Task(
        uid="a", status=status, completed=utc_now_epoch(), percent_complete=60
    ).canonicalized()
    assert task.completed is None
    assert task.percent_complete == 60


def test_cancelled_preserves_percent_complete():
    """User-confirmed decision: CANCELLED keeps PERCENT-COMPLETE as-is."""
    task = Task(
        uid="a", status=Status.CANCELLED, completed=utc_now_epoch(), percent_complete=42
    ).canonicalized()
    assert task.completed is None
    assert task.percent_complete == 42


def test_canonicalized_returns_a_copy():
    original = Task(uid="a", status=Status.COMPLETED, percent_complete=10)
    canonical = original.canonicalized()
    assert original.percent_complete == 10
    assert canonical.percent_complete == 100


def test_canonicalize_fields_ignores_a_status_less_update():
    """A summary-only edit must not clear a COMPLETED timestamp."""
    out = canonicalize_fields({"summary": "new title"})
    assert out == {"summary": "new title"}
    assert "completed" not in out


def test_canonicalize_fields_coerces_a_string_status():
    out = canonicalize_fields({"status": "COMPLETED", "percent_complete": 5})
    assert out["status"] is Status.COMPLETED
    assert out["percent_complete"] == 100


# -------------------------------------------------------------------- priority


def test_priority_zero_becomes_none():
    """RFC 5545: PRIORITY 0 means undefined, not highest."""
    assert Task(uid="a", priority=0).priority is None


def test_priority_absent_is_none():
    assert Task(uid="a").priority is None


def test_priority_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        Task(uid="a", priority=10)


@pytest.mark.parametrize(
    ("priority", "band"),
    [(1, "high"), (4, "high"), (5, "medium"), (6, "low"), (9, "low"), (None, None)],
)
def test_priority_bands(priority, band):
    assert Task(uid="a", priority=priority).priority_band == band


def test_percent_complete_is_clamped():
    assert Task(uid="a", percent_complete=140).percent_complete == 100
    assert Task(uid="a", percent_complete=-5).percent_complete == 0


# ---------------------------------------------------------------- read-only


def test_is_read_only_reflects_the_reason():
    assert not Task(uid="a").is_read_only
    assert Task(uid="a", read_only_reason="multipart").is_read_only


# ------------------------------------------------------- calendar identity


def test_calendar_id_is_deterministic_and_16_hex_chars():
    first = calendar_id("work", "/dav/user/tasks/")
    assert first == calendar_id("work", "/dav/user/tasks/")
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


def test_calendar_id_separates_remote_from_href():
    """The NUL separator stops 'a' + '/b' colliding with 'a/' + 'b'."""
    assert calendar_id("a", "/b") != calendar_id("a/", "b")


def test_calendar_create_derives_its_id():
    cal = Calendar.create("work", "/dav/user/tasks/", display_name="Tasks")
    assert cal.id == calendar_id("work", "/dav/user/tasks/")
    assert cal.display_name == "Tasks"


# --------------------------------------------------------------------- order


def test_rebalance_assigns_sparse_multiples():
    assert rebalance_orders([None, 1, 2]) == [1000, 2000, 3000]
